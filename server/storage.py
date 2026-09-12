"""Small, thread-safe SQLite document store. Provider secrets are encrypted at rest."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

COLLECTIONS = frozenset({"teams", "evidence", "events", "matches", "sources", "runs", "tracking", "candidates", "recommendations", "fixture_sources", "knowledge"})
DEFAULT_SETTINGS = {
    "mode": "demo", "followed_teams": ["arsenal"], "spoiler_free": True,
    "explanation_depth": "standard", "auto_refresh": False, "refresh_minutes": 30,
    "automation": {"enabled": False, "interval_minutes": 60, "sync_matches": True, "generate_brief": False},
    "verification_enabled": True, "verification_timeout_seconds": 45,
    "personalization": {"interests": [], "reading_language": "zh", "fan_level": "regular", "briefing_window_hours": 72},
    "llm": {"base_url": "", "model": "", "api_key": "", "max_rounds": 10,
            "timeout_seconds": 90, "temperature": 0.2, "protocol": "chat_completions",
            "send_temperature": False, "max_output_tokens": 3000, "use_env_proxy": True,
            "thinking_enabled": False},
}


def provider_origin(url):
    parsed = urlsplit(url or "")
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or (443 if parsed.scheme == "https" else 80))


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
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
        self._db.execute("CREATE TABLE IF NOT EXISTS documents (collection TEXT, id TEXT, body TEXT NOT NULL, PRIMARY KEY(collection,id))")
        self._db.execute("CREATE INDEX IF NOT EXISTS documents_scope ON documents(collection, json_extract(body, '$.mode'), json_extract(body, '$.team_id'))")
        self._db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS secrets (key TEXT PRIMARY KEY, encrypted BLOB NOT NULL)")
        self._db.commit()

    @contextmanager
    def atomic(self):
        with self.lock:
            yield

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
        return [json.loads(row[0]) for row in rows]

    def get(self, collection: str, id: str):
        self._collection(collection)
        with self.lock:
            row = self._db.execute("SELECT body FROM documents WHERE collection=? AND id=?", (collection, id)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, collection: str, item: dict):
        self._collection(collection)
        if not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("Document requires an id")
        body = json.dumps(item, ensure_ascii=False)
        with self.lock:
            self._db.execute("INSERT INTO documents(collection,id,body) VALUES(?,?,?) ON CONFLICT(collection,id) DO UPDATE SET body=excluded.body", (collection, item["id"], body))
            self._db.commit()
        return copy.deepcopy(item)

    def delete(self, collection: str, id: str):
        self._collection(collection)
        with self.lock:
            self._db.execute("DELETE FROM documents WHERE collection=? AND id=?", (collection, id))
            self._db.commit()

    def get_meta(self, key: str, default=None):
        with self.lock:
            row = self._db.execute("SELECT body FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else copy.deepcopy(default)

    def set_meta(self, key: str, value):
        with self.lock:
            self._db.execute("INSERT INTO metadata(key,body) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET body=excluded.body", (key, json.dumps(value, ensure_ascii=False)))
            self._db.commit()

    def settings(self):
        with self.lock:
            settings = copy.deepcopy(DEFAULT_SETTINGS)
            saved = self.get_meta("settings", {})
            settings.update({key: value for key, value in saved.items() if key not in {"llm", "automation", "personalization"}})
            settings["llm"].update(saved.get("llm", {}))
            settings["automation"].update(saved.get("automation", {}))
            settings["personalization"].update(saved.get("personalization", {}))
            row = self._db.execute("SELECT encrypted FROM secrets WHERE key='llm_api_key'").fetchone()
            settings["llm"]["api_key"] = self._cipher.decrypt(row[0]).decode() if row else ""
            return settings

    def save_settings(self, patch: dict):
        with self.lock:
            current = self.settings()
            current.update({key: value for key, value in patch.items() if key not in {"llm", "automation", "personalization"}})
            current["automation"].update(patch.get("automation", {}))
            current["personalization"].update(patch.get("personalization", {}))
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
            self._db.commit()
            return self.settings()

    def close(self):
        with self.lock:
            self._db.close()
