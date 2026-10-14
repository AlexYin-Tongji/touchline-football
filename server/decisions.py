"""Durable reasons and structured observations for team maintenance.

These are records of what we read, never assertions of truth or replacements for
source evidence. No article text, model transcript, credentials or arbitrary
provider payload is accepted into an observation.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from urllib.parse import urlsplit, urlunsplit
import uuid

FIELDS = {
    "roster": ("id", "provider_id", "name", "number", "position", "team_id", "season_id"),
    "schedule": ("id", "opponent_id", "competition_id", "kickoff", "status", "goals_for", "goals_against"),
    "standings": ("team_id", "rank", "played", "won", "drawn", "lost", "points", "goals_for", "goals_against"),
    "portraits": ("id",),
}
TERMINAL = {"completed", "partial", "failed", "interrupted", "checked", "deferred"}
# How many finished records one team keeps. Each row carries two full before/after
# observation sets and measures about 26 KB, so this is a size decision, not a
# history-length preference: at the old unbounded rate a single club had reached
# 64 rows, and every read of the collection decoded all of it.
#
# The cap is per team, because that is the axis a reader asks along — the
# decision log on a team page is that club's history, and one busy club must not
# push another club's records out.
#
# It is set to the largest page the history API will serve (``le=`` on
# ``/api/teams/{id}/decisions``), because a smaller cap would make that ceiling a
# lie: a caller asking for 50 would silently receive fewer. The reader shows five
# of them; the rest is headroom for a caller that asks for more.
RETAIN_PER_TEAM = 50


def _now():
    return datetime.now(timezone.utc).isoformat()


def _url(value):
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return ""
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:
        return ""


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def observations(view):
    from .provenance import tier_for_url
    part_sources = view.get("part_sources") or {}
    output = {}
    for part, fields in FIELDS.items():
        records = view.get("matches" if part == "schedule" else "roster" if part == "portraits" else part, []) or []
        if part == "portraits":
            records = [row for row in records if row.get("photo_url")]
        values = []
        for row in records:
            if not isinstance(row, dict):
                continue
            value = {key: str(row[key])[:200] if isinstance(row[key], str) else row[key]
                     for key in fields if key in row and isinstance(row[key], (str, int, float, bool, type(None)))}
            if part == "portraits":
                # Keep only a stable identifier in the observation; source URLs
                # are separately sanitized and never include query credentials.
                value["photo_hash"] = hashlib.sha256(_url(row.get("photo_url")).encode()).hexdigest()
            values.append(value)
        values.sort(key=lambda row: str(row.get("id", row.get("team_id", ""))))
        component = (view.get("components") or {}).get(part) or {}
        fetched = (view.get("roster_updated_at") if part == "roster" else view.get("standings_updated_at") if part == "standings" else None) or component.get("updated_at")
        if part == "portraits":
            urls = [row.get(field) for row in records if isinstance(row, dict)
                    for field in ("photo_source_url", "photo_file_page")]
        elif part_sources:
            urls = [source.get("url") for source in part_sources.get(part, []) if isinstance(source, dict)]
        elif part == "schedule":
            urls = [row.get("source_url") for row in records if isinstance(row, dict)]
        else:
            marker = {"roster": "阵容", "standings": "积分"}.get(part, "")
            urls = [source.get("url") for source in view.get("sources", []) if isinstance(source, dict)
                    and (marker in source.get("name", "") or part == "roster" and len(view.get("sources", [])) == 1)]
        sources = []
        for raw in urls:
            url = _url(raw)
            if url and url not in {source["url"] for source in sources}:
                tier = tier_for_url(url)
                sources.append({"url": url, "tier": tier.value if tier else "unknown"})
        output[part] = {"count": len(values), "values": values[:100], "truncated": len(values) > 100,
                        "fingerprint": hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                        "fetched_at": fetched, "observed_at": view.get("roster_source_updated_at") if part == "roster" else None,
                        "sources": sources}
    return output


def begin(store, team_id, view, *, parts, trigger, reason, plan=None):
    season = view.get("season") or {}
    stamp, identity = _now(), "decision_" + uuid.uuid4().hex
    row = {"id": identity, "mode": "live", "team_id": team_id, "season_id": season.get("id"),
           "season_label": season.get("label"), "kind": "team_refresh", "created_at": stamp,
           "trigger": "automatic" if trigger == "automatic" else "manual", "status": "running",
           "reason": str(reason)[:600], "parts": [part for part in parts if part in FIELDS],
           "before": observations(view), "plan": _json_safe(plan or {})}
    with store.atomic():
        prior = _rows(store, team_id, limit=1)
        row["previous_id"] = prior[0]["id"] if prior else None
        store.put("decisions", row)
    return identity


def finish(store, identity, view=None, outcome=None):
    outcome = outcome or {}
    with store.atomic():
        row = store.get("decisions", identity)
        if not row or row["status"] in TERMINAL:
            return row
        status = outcome.get("status", "failed")
        row.update(status=status if status in TERMINAL else "failed", finished_at=_now())
        if view is not None:
            row["after"] = observations(view)
            changes = []
            for part in row["parts"]:
                before, after = row["before"][part], row["after"][part]
                component_status = (outcome.get("components") or {}).get(part, {}).get("status", status)
                component = (outcome.get("components") or {}).get(part, {})
                confirmed = (row["status"] in {"completed", "partial"} and bool(after["fetched_at"])
                             and component_status not in {"failed", "interrupted", "unavailable"}
                             and component.get("cached") is not True
                             and after["fetched_at"] != before["fetched_at"])
                changes.append({"field": part, "changed": before["fingerprint"] != after["fingerprint"],
                                "confirmed": confirmed, "last_confirmed_at": after["fetched_at"],
                                "before_count": before["count"], "after_count": after["count"]})
            row["changes"] = changes
        # Error strings may contain upstream request details. Record outcomes by
        # reviewed status only; the existing component UI owns source diagnostics.
        row["summary"] = {"completed": "来源读取完成，已记录前后变化。", "partial": "部分来源未完成，已保留上次资料。",
                          "failed": "来源读取失败，未确认新的资料。", "interrupted": "更新中断，已有资料保留。",
                          "checked": "资料仍在时效内，本轮没有重新读取来源。",
                          "deferred": "已有任务或重试间隔尚未结束，本轮未重复请求。"}[row["status"]]
        store.put("decisions", row)
        _trim(store, row.get("team_id"))
        return row


def _trim(store, team_id, *, keep=RETAIN_PER_TEAM):
    """Drop a team's oldest *finished* records beyond the cap.

    Called from ``finish`` for the row just written, so the collection cannot
    grow without bound no matter which caller finishes a cycle. Only terminal
    records are candidates: a running row is the row ``finish`` itself is about
    to update, and deleting it would erase a decision that has not been recorded
    yet. The list query already orders newest-first, so anything past the cap is
    the oldest.

    Rows are deleted inside the same transaction as the write that triggered the
    trim, which is what makes the bound hold under concurrent writers: two
    finishers cannot each see a full list and leave one over the cap.
    """
    if not team_id:
        return 0
    # No lock of our own: this runs inside ``finish``'s transaction, which
    # already holds the store's re-entrant lock, and ``_commit`` inside a
    # transaction is a no-op by design. Adding either here would only obscure
    # which transaction the deletes belong to.
    rows = store._db.execute(
        "SELECT id, body FROM documents WHERE collection='decisions' "
        "AND json_extract(body, '$.team_id')=? ORDER BY rowid DESC",
        (team_id,)).fetchall()
    stale = [identity for identity, body in rows[max(1, int(keep)):]
             if _terminal_body(body)]
    if not stale:
        return 0
    store._db.executemany("DELETE FROM documents WHERE collection='decisions' AND id=?",
                          [(identity,) for identity in stale])
    store._commit()
    return len(stale)


def _terminal_body(body):
    """Whether a stored row's status is finished, without trusting the decode."""
    try:
        return json.loads(body).get("status") in TERMINAL
    except (TypeError, ValueError):
        # An unreadable body is not a record this module can vouch for; treating
        # it as retained keeps the trim from deleting something it cannot read.
        return False


def _rows(store, team_id, *, limit):
    with store.lock:
        rows = store._db.execute(
            "SELECT body FROM documents WHERE collection='decisions' AND json_extract(body, '$.team_id')=? ORDER BY rowid DESC LIMIT ?",
            (team_id, max(1, min(int(limit), 50)))).fetchall()
    return [json.loads(row[0]) for row in rows]


def history(store, team_id, *, limit=20, reveal=False):
    rows = _rows(store, team_id, limit=limit)
    protected = bool(store.settings().get("spoiler_free", True) and not reveal)
    for row in rows:
        row["spoiler_hidden"] = protected
        if protected:
            for phase in ("before", "after"):
                for part in ("schedule", "standings"):
                    observation = row.get(phase, {}).get(part)
                    if observation:
                        for key in ("values", "fingerprint", "count", "truncated"):
                            observation.pop(key, None)
            for change in row.get("changes", []):
                if change["field"] in {"schedule", "standings"}:
                    for key in ("changed", "before_count", "after_count"):
                        change.pop(key, None)
    return {"items": rows, "history_note": "仅记录此功能启用后的观察；没有记录的历史无法补回。"}


def recover(store):
    """Close out records left running by a crash, without reading the archive.

    ``store.list`` decodes every row in the collection — on a real database that
    was megabytes of JSON parsed to find the handful that were still running.
    The terminal statuses are known, so the unfinished ones can be selected in
    SQL and only those decoded.
    """
    placeholders = ",".join("?" for _ in TERMINAL)
    with store.lock:
        rows = store._db.execute(
            "SELECT id FROM documents WHERE collection='decisions' "
            f"AND json_extract(body, '$.status') NOT IN ({placeholders})",
            tuple(sorted(TERMINAL))).fetchall()
    for (identity,) in rows:
        finish(store, identity, outcome={"status": "interrupted"})
