"""Small, thread-safe SQLite document store. Provider secrets are encrypted at rest."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

COLLECTIONS = frozenset({"teams", "evidence", "events", "matches", "sources", "runs", "tracking", "candidates", "recommendations", "fixture_sources", "knowledge", "leagues", "seasons", "league_memberships", "league_matches", "standings", "reports", "model_profiles", "players", "squads", "team_snapshots", "agent_tasks", "decisions"})
DEFAULT_SETTINGS = {
    "mode": "live", "followed_teams": ["arsenal"], "spoiler_free": True,
    "explanation_depth": "standard", "auto_refresh": False, "refresh_minutes": 30,
    "automation": {"enabled": False, "interval_minutes": 60, "sync_matches": True, "generate_brief": False},
    "verification_enabled": True, "verification_timeout_seconds": 45,
    "personalization": {"interests": [], "reading_language": "zh", "fan_level": "regular", "briefing_window_hours": 72},
    "reports": {"auto_generate": True},
    "llm": {"base_url": "", "model": "", "api_key": "", "max_rounds": 10,
            "timeout_seconds": 90, "temperature": 0.2, "protocol": "chat_completions",
            "send_temperature": False, "max_output_tokens": 16000, "use_env_proxy": True,
            "thinking_enabled": False},
}


def provider_origin(url):
    parsed = urlsplit(url or "")
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or (443 if parsed.scheme == "https" else 80))


class Store:
    # A queued editorial run may use a 30-minute task window. Retain bodies in
    # memory across that window; capacity eviction and restart still require a
    # fresh read, and no publisher body is written to SQLite.
    BODY_TTL_SECONDS = 60 * 60
    BODY_CACHE_BYTES = 4 * 1024 * 1024
    BODY_CACHE_ITEMS = 96

    def __init__(self, path: str | Path):
        self.path = Path(path)
        existing_database = self.path.is_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._bodies = OrderedDict()
        self._transaction_depth = 0
        key_path = self.path.with_suffix(".key")
        try:
            with key_path.open("xb") as key_file:
                os.chmod(key_path, 0o600)
                key_file.write(Fernet.generate_key())
        except FileExistsError:
            pass
        self._cipher = Fernet(key_path.read_bytes())
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA secure_delete=ON")
        self._db.execute("CREATE TABLE IF NOT EXISTS documents (collection TEXT, id TEXT, body TEXT NOT NULL, PRIMARY KEY(collection,id))")
        self._db.execute("CREATE INDEX IF NOT EXISTS documents_scope ON documents(collection, json_extract(body, '$.mode'), json_extract(body, '$.team_id'))")
        self._db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS secrets (key TEXT PRIMARY KEY, encrypted BLOB NOT NULL)")
        self._db.commit()
        try:
            old_reports = self._db.execute("SELECT 1 FROM documents WHERE collection='reports' AND json_extract(body, '$.schema_version') IS NOT 2 AND json_extract(body, '$.boundary_migration.version') IS NOT 2 LIMIT 1").fetchone()
            if existing_database and (not self.get_meta("transient_bodies_v1", False) or old_reports):
                self.backup("before-migration")
            from .news_reports import migrate_report_boundaries
            with self.atomic():
                removed_bodies = self._migrate_external_bodies()
                migrate_report_boundaries(self)
            if removed_bodies:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._db.execute("VACUUM")
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except BaseException:
            self._db.close()
            raise

    def backup(self, reason="manual"):
        """Keep a consistent, private database and its encryption key together."""
        directory = self.path.parent / "backups"
        directory.mkdir(exist_ok=True)
        target_path = directory / f"{self.path.stem}-{reason}-{time.time_ns()}.sqlite3"
        with self.lock:
            target = sqlite3.connect(target_path)
            try:
                self._db.backup(target)
            finally:
                target.close()
            shutil.copyfile(self.path.with_suffix(".key"), target_path.with_suffix(".key"))
            os.chmod(target_path, 0o600)
            os.chmod(target_path.with_suffix(".key"), 0o600)
        return target_path

    def _commit(self):
        if not self._transaction_depth:
            self._db.commit()

    @staticmethod
    def external_evidence(item):
        return item.get("mode") == "live" and item.get("origin") in {"web", "rss"} and bool(item.get("url")) and not item.get("fixture_record")

    def _trim_bodies(self):
        current = time.monotonic()
        for key, (until, body) in list(self._bodies.items()):
            if until <= current:
                self._bodies.pop(key, None)
        while (len(self._bodies) > self.BODY_CACHE_ITEMS or
               sum(len(body.encode("utf-8")) for _, body in self._bodies.values()) > self.BODY_CACHE_BYTES):
            self._bodies.popitem(last=False)

    def _prepare(self, collection, item):
        item = copy.deepcopy(item)
        if collection == "candidates":
            item.pop("text", None)
            if "description" in item:
                item["description"] = str(item["description"] or "")[:280]
        if collection == "evidence" and self.external_evidence(item):
            body = item.pop("text", "")
            item.pop("paragraphs", None)
            item.pop("body_available", None)
            if body:
                item["body_hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
                item["body_characters"] = len(body)
                from .ingestion import safe_evidence_view
                safe = safe_evidence_view({**item, "text": body}) if item.get("spoiler") else None
                if safe:
                    item["safe_view_meta"] = {"body_hash": hashlib.sha256(safe["text"].encode()).hexdigest(),
                                              "redacted_paragraph_count": safe.get("redacted_paragraph_count", 0)}
                else:
                    item.pop("safe_view_meta", None)
                self._bodies[(item["id"], item["body_hash"])] = (time.monotonic() + self.BODY_TTL_SECONDS, body)
                self._trim_bodies()
            item["body_storage"] = "transient"
        return item

    def _hydrate(self, collection, item):
        if collection == "evidence" and self.external_evidence(item) and item.get("body_storage") == "transient":
            self._trim_bodies()
            body = self._bodies.get((item["id"], item.get("body_hash")))
            item["text"] = body[1] if body else ""
            item["body_available"] = bool(body)
        return item

    def _migrate_external_bodies(self):
        """Remove publisher body copies once, retaining personal imports and notes.

        Keep hash-only provenance for the verified-delivery migration that runs
        next. Removing an article body alone never grants an old report validity.
        """
        if self.get_meta("transient_bodies_v1", False):
            return
        from .team_profiles import _evidence_hash
        from .news_reports import _hash, source_version_hash
        rows = self.list("evidence")
        old_hashes = {row["id"]: _evidence_hash(row, legacy=True) for row in rows}
        with self.atomic():
            for row in rows:
                if self.external_evidence(row):
                    clean = self._prepare("evidence", row)
                    self._db.execute("UPDATE documents SET body=? WHERE collection='evidence' AND id=?", (json.dumps(clean, ensure_ascii=False), row["id"]))
        self._bodies.clear()
        new_hashes = {row["id"]: _evidence_hash(row) for row in self.list("evidence")}
        proofs = self.get_meta("legacy_source_provenance_v1", {})
        for row in rows:
            proofs[row["id"]] = {"legacy_record_hash": _hash(row), "stable_record_hash": source_version_hash(self.get("evidence", row["id"]))}
        self.set_meta("legacy_source_provenance_v1", proofs)
        for run in self.list("runs"):
            hashes = run.get("evidence_hashes") or {}
            if any(eid in old_hashes and value == old_hashes[eid] for eid, value in hashes.items()):
                run["evidence_hashes"] = {eid: new_hashes[eid] if eid in old_hashes and value == old_hashes[eid] else value for eid, value in hashes.items()}
                self.put("runs", run)
        for knowledge in self.list("knowledge"):
            changed = False
            for source in knowledge.get("sources", []):
                eid = source.get("id")
                if eid in old_hashes and source.get("content_hash") == old_hashes[eid]:
                    source["content_hash"] = new_hashes[eid]
                    changed = True
            if changed:
                self.put("knowledge", knowledge)
        for candidate in self.list("candidates"):
            self.put("candidates", candidate)
        for report in self.list("reports"):
            if report.get("source_excerpt"):
                self.put("reports", {**report, "source_excerpt": ""})
        for (key,) in self._db.execute("SELECT key FROM metadata WHERE key LIKE 'news_index:%'").fetchall():
            self.set_meta(key, self.get_meta(key))
        self.set_meta("transient_bodies_v1", True)
        return any(self.external_evidence(row) for row in rows)

    @contextmanager
    def atomic(self):
        """Synchronous nested transaction; callers must not await inside it."""
        with self.lock:
            savepoint = f"store_{self._transaction_depth}"
            bodies = self._bodies.copy()
            self._db.execute(f"SAVEPOINT {savepoint}")
            self._transaction_depth += 1
            try:
                yield
            except BaseException:
                self._db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._db.execute(f"RELEASE SAVEPOINT {savepoint}")
                self._bodies = bodies
                raise
            else:
                self._db.execute(f"RELEASE SAVEPOINT {savepoint}")
            finally:
                self._transaction_depth -= 1

    @staticmethod
    def _collection(collection):
        if collection not in COLLECTIONS:
            raise ValueError("Unknown collection")

    def list(self, collection: str, mode: str | None = None, team_id: str | None = None):
        self._collection(collection)
        conditions, values = ["collection=?"], [collection]
        if mode is not None:
            conditions.append("json_extract(body, '$.mode')=?")
            values.append(mode)
        if team_id is not None:
            conditions.append("json_extract(body, '$.team_id')=?")
            values.append(team_id)
        with self.lock:
            rows = self._db.execute("SELECT body FROM documents WHERE " + " AND ".join(conditions) + " ORDER BY rowid", values).fetchall()
        with self.lock:
            return [self._hydrate(collection, json.loads(row[0])) for row in rows]

    def get(self, collection: str, id: str):
        self._collection(collection)
        with self.lock:
            row = self._db.execute("SELECT body FROM documents WHERE collection=? AND id=?", (collection, id)).fetchone()
            return self._hydrate(collection, json.loads(row[0])) if row else None

    def count(self, collection: str, mode: str | None = None):
        self._collection(collection)
        query, values = "SELECT count(*) FROM documents WHERE collection=?", [collection]
        if mode is not None:
            query += " AND json_extract(body, '$.mode')=?"
            values.append(mode)
        with self.lock:
            return self._db.execute(query, values).fetchone()[0]

    def put(self, collection: str, item: dict):
        self._collection(collection)
        if not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("Document requires an id")
        with self.lock:
            body = json.dumps(self._prepare(collection, item), ensure_ascii=False)
            self._db.execute("INSERT INTO documents(collection,id,body) VALUES(?,?,?) ON CONFLICT(collection,id) DO UPDATE SET body=excluded.body", (collection, item["id"], body))
            self._commit()
        return copy.deepcopy(item)

    def delete(self, collection: str, id: str):
        self._collection(collection)
        with self.lock:
            self._db.execute("DELETE FROM documents WHERE collection=? AND id=?", (collection, id))
            self._commit()

    def put_many(self, collection: str, items: list[dict]):
        """Commit a validated provider snapshot together, never a partial table."""
        self._collection(collection)
        if any(not isinstance(item.get("id"), str) or not item["id"] for item in items):
            raise ValueError("Document requires an id")
        with self.atomic():
            rows = [(collection, item["id"], json.dumps(self._prepare(collection, item), ensure_ascii=False)) for item in items]
            self._db.executemany("INSERT INTO documents(collection,id,body) VALUES(?,?,?) ON CONFLICT(collection,id) DO UPDATE SET body=excluded.body", rows)

    def get_meta(self, key: str, default=None):
        with self.lock:
            row = self._db.execute("SELECT body FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else copy.deepcopy(default)

    def get_secret(self, name: str) -> str:
        with self.lock:
            row = self._db.execute("SELECT encrypted FROM secrets WHERE key=?", (name,)).fetchone()
            return self._cipher.decrypt(row[0]).decode() if row else ""

    def set_secret(self, name: str, value: str | None):
        with self.lock:
            if value:
                self._db.execute("INSERT INTO secrets(key,encrypted) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET encrypted=excluded.encrypted", (name, self._cipher.encrypt(value.encode())))
            else:
                self._db.execute("DELETE FROM secrets WHERE key=?", (name,))
            self._commit()

    def set_meta(self, key: str, value):
        if key.startswith("news_index:") and isinstance(value, dict):
            value = copy.deepcopy(value)
            for entry in value.get("entries", []):
                if isinstance(entry, dict):
                    entry.pop("text", None)
                    if "description" in entry:
                        entry["description"] = str(entry["description"] or "")[:280]
        with self.lock:
            self._db.execute("INSERT INTO metadata(key,body) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET body=excluded.body", (key, json.dumps(value, ensure_ascii=False)))
            self._commit()

    def settings(self):
        with self.lock:
            settings = copy.deepcopy(DEFAULT_SETTINGS)
            saved = self.get_meta("settings", {})
            settings.update({key: value for key, value in saved.items() if key not in {"llm", "automation", "personalization", "reports"}})
            settings["llm"].update(saved.get("llm", {}))
            settings["automation"].update(saved.get("automation", {}))
            settings["personalization"].update(saved.get("personalization", {}))
            settings["reports"].update(saved.get("reports", {}))
            row = self._db.execute("SELECT encrypted FROM secrets WHERE key='llm_api_key'").fetchone()
            settings["llm"]["api_key"] = self._cipher.decrypt(row[0]).decode() if row else ""
            return settings

    def save_settings(self, patch: dict):
        with self.lock:
            current = self.settings()
            current.update({key: value for key, value in patch.items() if key not in {"llm", "automation", "personalization", "reports"}})
            current["automation"].update(patch.get("automation", {}))
            current["personalization"].update(patch.get("personalization", {}))
            current["reports"].update(patch.get("reports", {}))
            llm = copy.deepcopy(patch.get("llm", {}))
            key = llm.pop("api_key", "")
            clear = llm.pop("clear_api_key", False)
            llm.pop("api_key_set", None)
            if "base_url" in llm and provider_origin(llm["base_url"]) != provider_origin(current["llm"]["base_url"]) and not key:
                clear = True
            if clear:
                self._db.execute("DELETE FROM secrets WHERE key='llm_api_key'")
            elif key:
                encrypted = self._cipher.encrypt(key.encode())
                self._db.execute("INSERT INTO secrets(key,encrypted) VALUES('llm_api_key',?) ON CONFLICT(key) DO UPDATE SET encrypted=excluded.encrypted", (encrypted,))
            current["llm"].update(llm)
            current["llm"].pop("api_key", None)
            self.set_meta("settings", current)
            self._commit()
            return self.settings()

    def close(self):
        with self.lock:
            self._db.close()
