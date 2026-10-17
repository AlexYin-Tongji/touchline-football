"""Ask a model which missing players are worth checking first.

Measured on a real squad, every player without a photo carried
``not_yet_resolved`` — not one had been rejected by a source. The batch loop
sizes a pass at twelve against a squad of thirty, and its cursor is pure
rotation, so it cannot express "these twelve are the ones worth asking about".
That is the whole of the gap this closes.

What the model may do here is narrow on purpose. It returns a list of player
ids already present in the local queue, and nothing else: no field carries a
photo address, a licence, or an identity claim, and the photo fields on a row
are still written only by ``_apply_photo`` from a source that proved the
identity. Asking *who* is a scheduling question; deciding *whether a face is
that player* is an evidence question, and it stays with the sources.

This is a single bounded request, not a multi-round tool loop. A team cycle can
run on a schedule, so an unbounded conversation with a model would put an
unpredictable paid cost behind an unattended timer.
"""
from __future__ import annotations

import json
import math
import re
import time

from .model_config import AgentFailure, background_config
from .provider import request_completion

# The batch the executor will actually read in one pass, from team_portraits.
MAX_BATCH = 12
_SYSTEM = (
    "你在安排一次球员照片核对。你会收到某个球队尚未取得照片的球员清单，每人的编号、位置、"
    "以及本地是否已有国籍和出生日期。\n"
    "照片来源只能按姓名加国籍、或姓名加出生日期来核对身份，因此拥有这些信息的球员更可能核对成功。\n"
    "请选出一批最值得先核对的球员并给出顺序。只能使用清单中已有的球员编号，不要编造编号，"
    "不要新增球员，不要输出任何照片地址或身份判断。\n"
    "如果清单为空，返回空列表。"
)
_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_photo_batch",
        "description": "提交本轮先核对的球员编号及顺序。",
        "parameters": {
            "type": "object",
            "properties": {
                "player_ids": {"type": "array", "items": {"type": "string"},
                               "minItems": 0, "maxItems": MAX_BATCH,
                               "description": "从清单中选出的球员编号，按核对优先顺序排列。"},
                "reason": {"type": "string", "maxLength": 300,
                           "description": "一句话说明排序依据，不包含任何球员个人资料。"},
            },
            "required": ["player_ids"],
            "additionalProperties": False,
        },
    },
}


def planner_config(store):
    """Config for the photo planner, or None when that pass is switched off.

    It never falls back to the active model on its own. Photo planning is a paid
    call that runs on a team refresh, so with one model configured it would start
    happening every time a team page opens. That is now a switch the reader sets
    (``background_model_tasks.photos``), not a role that happens to be unset.
    """
    try:
        return background_config(store, "photos")
    except AgentFailure:
        return None


def _queue_payload(report):
    """The slice of the queue a planner may see — no photo or identity data."""
    return {"team_id": report.get("team_id"), "total": report.get("total"),
            "available": report.get("available"), "missing": report.get("missing"),
            "untried_count": report.get("untried_count"),
            "players": [{"player_id": entry["player_id"], "name": entry["name"],
                         "position_group": entry["position_group"],
                         "has_nationality": entry["has_nationality"],
                         "has_birth_date": entry["has_birth_date"]}
                        for entry in report.get("untried", [])],
            "note": report.get("hint")}


def _extract(message, allowed):
    """Ids from the tool call, kept only if the queue actually contains them."""
    calls = message.get("tool_calls") or []
    call = next((item for item in calls
                 if (item.get("function") or {}).get("name") == "submit_photo_batch"), None)
    if call is None:
        return []
    try:
        arguments = json.loads((call.get("function") or {}).get("arguments") or "{}")
    except (TypeError, ValueError):
        return []
    ids = arguments.get("player_ids")
    if not isinstance(ids, list) or len(ids) > MAX_BATCH:
        return []
    chosen = []
    for value in ids:
        identity = str(value)
        # An id the queue does not list is dropped, not trusted: the model may
        # only order work that already exists.
        if identity in allowed and identity not in chosen:
            chosen.append(identity)
    return chosen


async def ordered_batch(store, report, *, requester=None, timeout=45):
    """A batch order for the players in ``report``, or None if no model is set.

    Any failure returns None, which means "use the ordinary cursor order". A
    planner that cannot answer must not stop a team refresh.
    """
    config = planner_config(store)
    if config is None:
        return None
    candidates = report.get("untried") or []
    if not candidates:
        return []
    allowed = {str(entry["player_id"]) for entry in candidates}
    messages = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": json.dumps(_queue_payload(report), ensure_ascii=False)}]
    duration = max(1.0, min(float(timeout), 120.0))
    if isinstance(timeout, bool) or not math.isfinite(duration):
        duration = 45.0
    request = requester or _default_requester
    try:
        message = await request(config, messages, duration)
    except Exception:
        return None
    chosen = _extract(message, allowed)
    if not chosen:
        return None
    return chosen


async def _default_requester(config, messages, duration):
    config = {**config, "_session_id": "photo_plan"}
    return await request_completion(
        config, messages, tools=[_TOOL],
        tool_choice={"type": "function", "function": {"name": "submit_photo_batch"}},
        timeout=duration)
