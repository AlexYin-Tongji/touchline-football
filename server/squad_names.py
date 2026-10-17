"""Mainland Chinese names for one roster, translated in a single model batch.

``squad.names`` owns this. It does not open Wikipedia, Wikidata, or any other
source, and it does not call the maintenance names pass. A row that already
has ``name_zh`` is left alone. A row with no English display name, or no
``provider_id`` to write back to, is recorded as ``MISSING`` and is not sent.
The model returns a Chinese name per ``provider_id``; that field is what gets
stored.
"""
from __future__ import annotations

import json

from .ingestion import now
from .model_config import AgentFailure, effective_config, role_selection
from .player_names import cjk_label, to_simplified
from .provider import request_completion

SOURCE = "模型译名"
_TOOL_NAME = "submit_squad_names"
_SYSTEM = (
    "你在给一份足球阵容补中国大陆常用译名。你会收到若干球员，每人有 provider_id 和英文显示名。\n"
    "只根据这些英文名给出大陆媒体常用的中文译名，使用简体中文。不要查资料，不要访问网页，"
    "不要根据俱乐部、国籍或号码猜测身份，不要编造名单以外的人。\n"
    "每个人最多返回一次。provider_id 必须来自清单。没有把握就不要返回这个人。"
)
_TOOL = {
    "type": "function",
    "function": {
        "name": _TOOL_NAME,
        "description": "按 provider_id 提交本批球员的简体中文译名。",
        "parameters": {
            "type": "object",
            "properties": {
                "names": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "provider_id": {"type": "string"},
                            "name_zh": {"type": "string"},
                        },
                        "required": ["provider_id", "name_zh"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["names"],
            "additionalProperties": False,
        },
    },
}


def storage_key(team_id, season_id):
    return f"squad_names_v1_{team_id}_{season_id}"


def load_record(store, team_id, season_id):
    record = store.get_meta(storage_key(team_id, season_id), None)
    players = record.get("players") if isinstance(record, dict) else None
    if not isinstance(players, dict):
        return {"players": {}}
    return {"players": dict(players)}


def _blank(value):
    return str(value or "").strip()


def accept_name(value):
    """A simplified Chinese label, or empty when the text is not a name."""
    simplified = to_simplified(value) if isinstance(value, str) else ""
    if cjk_label(simplified):
        return simplified
    return ""


def attach_model_names(store, team_id, season_id, roster):
    """Fill empty ``name_zh`` from the stored translation. Sourced names stay.

    A stored reading applies only when the English display name still matches.
    A roster rebuild that changes the spelling does not keep the old translation.
    """
    players = load_record(store, team_id, season_id)["players"]
    for row in roster or []:
        if not isinstance(row, dict) or _blank(row.get("name_zh")):
            continue
        stored = players.get(_blank(row.get("provider_id")))
        if not isinstance(stored, dict) or _blank(stored.get("name")) != _blank(row.get("name")):
            continue
        name_zh = accept_name(stored.get("name_zh"))
        if name_zh:
            row["name_zh"] = name_zh
            row["name_zh_source"] = SOURCE
            row.pop("name_zh_reason_code", None)
            row.pop("name_zh_retryable", None)
        elif stored.get("reason_code"):
            row["name_zh_reason_code"] = stored["reason_code"]
            row["name_zh_retryable"] = bool(stored.get("retryable"))
    return roster


def apply_stored_names(store, snapshot):
    """Reattach stored translations onto a snapshot before it is written back."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("roster"), list):
        return snapshot
    attach_model_names(store, snapshot.get("team_id"), snapshot.get("season_id"), snapshot["roster"])
    return snapshot


def remember(store, team_id, season_id, rows):
    """Store translations or skip reasons, and mirror them onto the snapshot."""
    record = load_record(store, team_id, season_id)
    updates = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        provider_id = _blank(row.get("provider_id"))
        english = _blank(row.get("name"))
        if not provider_id:
            continue
        name_zh = accept_name(row.get("name_zh"))
        entry = {"name": english, "checked_at": row.get("checked_at") or now()}
        if name_zh:
            entry.update(name_zh=name_zh, reason_code=None, retryable=False)
        else:
            entry.update(name_zh="", reason_code=row.get("reason_code") or "MISSING",
                         retryable=bool(row.get("retryable")))
        record["players"][provider_id] = entry
        updates[provider_id] = entry
    store.set_meta(storage_key(team_id, season_id), record)
    _mirror(store, team_id, season_id, updates)
    return record


def _mirror(store, team_id, season_id, updates):
    from .team_center import _snapshot_id
    snapshot_id = _snapshot_id(team_id, season_id)
    snapshot = store.get("team_snapshots", snapshot_id)
    if not snapshot or snapshot.get("team_id") != team_id or not updates:
        return
    for row in snapshot.get("roster") or []:
        if not isinstance(row, dict):
            continue
        stored = updates.get(_blank(row.get("provider_id")))
        if not stored or _blank(stored.get("name")) != _blank(row.get("name")):
            continue
        if stored.get("name_zh"):
            row["name_zh"] = stored["name_zh"]
            row["name_zh_source"] = SOURCE
            row.pop("name_zh_reason_code", None)
            row.pop("name_zh_retryable", None)
        else:
            row.pop("name_zh", None)
            row.pop("name_zh_source", None)
            row["name_zh_reason_code"] = stored.get("reason_code")
            row["name_zh_retryable"] = bool(stored.get("retryable"))
    store.put("team_snapshots", snapshot)


def names_config(store):
    """The team role, or None when no model is assigned.

    No model means the leaf records ``MODEL_FAILED``. It does not fall through
    to a network call, and it does not invent a name.
    """
    selected = role_selection(store, "team")
    if not selected:
        return None
    try:
        return effective_config(store, selected["provider_id"], selected["model"])
    except AgentFailure:
        return None


def _extract(message, allowed):
    """The names the model returned, plus whether it answered at all.

    The second value is the whole point of this function, and it used to be
    missing. An empty tool call and a failed call both produce no names, so
    ``_extract`` returned the same ``{}`` for "the model looked at these and
    declined" and for "the model never got the question" — and the caller then
    recorded both as ``MODEL_FAILED`` and retried them forever.

    The system prompt tells the model not to return a person it is unsure of,
    so declining is the behaviour we asked for. A squad with five unguessable
    academy names therefore never settled: every round re-asked the same five,
    spent a request, and stored the same failure. Distinguishing them is what
    makes the loop converge.
    """
    calls = message.get("tool_calls") if isinstance(message, dict) else None
    call = next((item for item in calls or []
                 if (item.get("function") or {}).get("name") == _TOOL_NAME), None)
    if call is None:
        return {}, False
    try:
        arguments = json.loads((call.get("function") or {}).get("arguments") or "{}")
    except (TypeError, ValueError):
        return {}, False
    rows = arguments.get("names")
    if not isinstance(rows, list):
        return {}, False
    chosen = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        provider_id = _blank(row.get("provider_id"))
        name_zh = accept_name(row.get("name_zh"))
        if provider_id in allowed and name_zh and provider_id not in chosen:
            chosen[provider_id] = name_zh
    return chosen, True


async def _default_requester(config, messages, duration):
    config = {**config, "_session_id": "squad_names"}
    return await request_completion(
        config, messages, tools=[_TOOL],
        tool_choice={"type": "function", "function": {"name": _TOOL_NAME}},
        timeout=duration)


async def translate_roster(store, team_id, *, requester=None):
    """Translate roster rows that still have no Chinese name.

    Returns without a model call when there is nothing to send. A missing
    English name is stored as ``MISSING`` and is not retryable. A model that
    is absent or that fails to name someone is ``MODEL_FAILED`` and is retryable.
    """
    from .team_center import team_season_context
    try:
        _, _, season, _ = team_season_context(store, team_id)
    except Exception:
        return {"status": "completed", "translated": 0, "pending": 0, "model_failed": 0}
    season_id = season["id"]
    from .team_center import _snapshot_id
    snapshot = store.get("team_snapshots", _snapshot_id(team_id, season_id)) or {}
    if snapshot.get("team_id") != team_id:
        snapshot = {}
    roster = attach_model_names(store, team_id, season_id,
                                [dict(row) for row in snapshot.get("roster") or [] if isinstance(row, dict)])
    pending = []
    skipped = []
    declined = 0
    for row in roster:
        if _blank(row.get("name_zh")):
            continue
        provider_id = _blank(row.get("provider_id"))
        english = _blank(row.get("name"))
        if not provider_id or not english:
            skipped.append({"provider_id": provider_id, "name": english,
                            "reason_code": "MISSING", "retryable": False})
            continue
        # A player the model already answered about stays out of the next batch.
        # Without this the pass rebuilt an identical queue every round — those
        # rows have no ``name_zh``, so nothing else excluded them — and the same
        # question was paid for again and again without any chance of a
        # different answer.
        if _blank(row.get("name_zh_reason_code")) == "MODEL_DECLINED":
            declined += 1
            continue
        pending.append({"provider_id": provider_id, "name": english})
    if skipped:
        remember(store, team_id, season_id, skipped)
    if not pending:
        return {"status": "completed", "translated": 0, "pending": 0, "model_failed": 0,
                "declined": declined, "skipped": len(skipped)}
    config = names_config(store)
    if config is None:
        _fail(store, team_id, season_id, pending)
        return {"status": "completed", "translated": 0, "pending": len(pending),
                "model_failed": len(pending), "skipped": len(skipped)}
    messages = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": json.dumps({"players": pending}, ensure_ascii=False)}]
    request = requester or _default_requester
    try:
        message = await request(config, messages, 60)
    except Exception:
        _fail(store, team_id, season_id, pending)
        return {"status": "completed", "translated": 0, "pending": len(pending),
                "model_failed": len(pending), "skipped": len(skipped)}
    chosen, answered = _extract(message, {row["provider_id"] for row in pending})
    written = []
    declined = []
    failed = []
    for row in pending:
        name_zh = chosen.get(row["provider_id"])
        if name_zh:
            written.append({**row, "name_zh": name_zh})
        elif answered:
            # The model saw this player and chose not to name them, which is the
            # instruction it was given for a name it cannot place. That is an
            # answer, not a failure: recording it as retryable is what made this
            # leaf re-ask the same unguessable players every single round.
            declined.append(row)
        else:
            failed.append(row)
    if written:
        remember(store, team_id, season_id, written)
    if declined:
        _decline(store, team_id, season_id, declined)
    if failed:
        _fail(store, team_id, season_id, failed)
    return {"status": "completed", "translated": len(written),
            "declined": len(declined), "pending": len(failed),
            "model_failed": len(failed), "skipped": len(skipped)}


def _fail(store, team_id, season_id, rows):
    remember(store, team_id, season_id, [
        {**row, "name_zh": "", "reason_code": "MODEL_FAILED", "retryable": True} for row in rows])


def _decline(store, team_id, season_id, rows):
    """The model answered and left these players unnamed.

    Stored as its own terminal code rather than as a failure. The distinction is
    the difference between a loop that settles and one that spends a request
    every round on a question already answered: a youth player no published
    source covers will not become nameable by being asked again tomorrow.
    """
    remember(store, team_id, season_id, [
        {**row, "name_zh": "", "reason_code": "MODEL_DECLINED", "retryable": False} for row in rows])
