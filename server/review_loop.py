"""One review loop, shared by every domain where code stops short of certainty.

The resolvers in this application are rules: a name matches or it does not, a
birth date agrees or it does not, a source has a record or it does not. That is
what makes them worth trusting, and it is also where they all stop. What is left
after the rules have run is a residue of *judgement* items:

* two candidates that every rule treats as equally good, so the choice is a
  judgement about which is more likely rather than a fact;
* a gap whose cause the code can narrow but not settle — a throttle and a wrong
  search term look the same from the reason code alone;
* something that looks wrong and cannot be adjudicated at all, which is a thing
  a person should look at rather than a rule should decide.

A model is good at exactly that residue and bad at everything the rules already
do. So the split is: **code decides what is true, what is missing and what has
already been answered; the model decides what is ambiguous, what is worth another
attempt, and how that attempt should be made.** This module is that second half,
and it is deliberately domain-agnostic — photos, club records and Chinese names
all feed the same loop, because they all have the same shape of residue.

What the model may never do here, in any domain:

* write a field. It returns a decision, never a value; every write still happens
  through the source-backed collector that owns it.
* introduce a source, an address or an identity claim. It chooses among the items
  the caller put on the board, and an id that is not on the board is dropped.
* widen its own input. The board is built by the caller from code's own findings,
  and the same rule that keeps a queue listing free of photo addresses keeps a
  review payload free of them.
"""
from __future__ import annotations

import json
import math

from .model_config import AgentFailure, effective_config, role_selection
from .provider import request_completion

# What the loop may decide about one item. These are actions, not adjectives:
# a stored "bad" would be a verdict nobody can execute, while "retry with a
# different search term" is a next step.
ACTION_KEEP = "keep"            # nothing to do; code's answer stands
ACTION_RETRY = "retry"          # worth another attempt, and how
ACTION_INVESTIGATE = "investigate"   # a person should look at this
ACTIONS = frozenset({ACTION_KEEP, ACTION_RETRY, ACTION_INVESTIGATE})
# How hard to push a retry. The caller maps this onto whatever its own backoff
# understands, so the loop never has to know a transport's policy.
EFFORT_NOW = "now"
EFFORT_LATER = "later"
EFFORTS = frozenset({EFFORT_NOW, EFFORT_LATER})

# One request covers a bounded board. A team cycle runs on a timer, so an
# unbounded conversation would put an unpredictable paid cost behind a schedule.
MAX_ITEMS = 12

_SYSTEM = (
    "你在复核一套本地资料系统的核对结果，并决定下一轮怎么推进。\n\n"
    "事实判定已经由代码完成：谁是谁、字段有没有、来源有没有回答过，都已经有结论。"
    "你要处理的是代码无法确定的模糊项——多个候选分不出高下、一个缺口看不出成因、"
    "或者某个结果看起来有问题但代码判不了。\n\n"
    "对每一项给出一个 action：\n"
    "- keep：代码的结论成立，不需要动作。\n"
    "- retry：值得再试一次，并用 retry_hint 说明该换什么（换来源、换检索词、换时间、"
    "先补哪个缺失字段），用 effort 说明是现在还是稍后。\n"
    "- investigate：有重大怀疑或信息互相矛盾，需要人工或进一步核查，不要自己下结论。\n\n"
    "只能使用清单中已有的条目编号。不要编造编号，不要提供或改写任何字段值，"
    "不要提出清单之外的新来源，也不要把不确定的推断写成结论。"
)


def _tool():
    """The one submission shape, domain-agnostic on purpose."""
    return {
        "type": "function",
        "function": {
            "name": "submit_review",
            "description": "提交本轮复核结论与下一步动作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "maxItems": MAX_ITEMS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "item_id": {"type": "string", "description": "清单中的条目编号。"},
                                "action": {"type": "string", "enum": sorted(ACTIONS)},
                                "effort": {"type": "string", "enum": sorted(EFFORTS)},
                                "reason": {"type": "string", "maxLength": 200,
                                           "description": "一句话说明依据，只描述已给定的事实。"},
                                "retry_hint": {"type": "string", "maxLength": 120,
                                               "description": "action=retry 时说明该换什么再试。"},
                            },
                            "required": ["item_id", "action"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["items"],
                "additionalProperties": False,
            },
        },
    }


def review_config(store):
    """Config for the review role, or None when no model is assigned to it.

    Returning None rather than reusing the active chat model is deliberate, and
    the same rule the batch planner follows: review calls run on a refresh that
    the reader did not initiate, so a reader who configured only a chat model
    must not silently start paying for them.
    """
    selected = role_selection(store, "review")
    if not selected:
        return None
    try:
        return effective_config(store, selected["provider_id"], selected["model"])
    except AgentFailure:
        return None


def board(items):
    """The payload, reduced to what the model may see.

    Every field is named here rather than passed through, so a caller cannot
    widen the payload by adding a key upstream. That is the same reason the photo
    queue carries an explicit column list: the model's input is a decision to be
    reviewed, not an object graph to be trusted.
    """
    return {"items": [{"item_id": str(item.get("item_id")), "domain": item.get("domain"),
                       "name": item.get("name"), "state": item.get("state"),
                       "reason_code": item.get("reason_code"), "detail": item.get("detail"),
                       "attempts": item.get("attempts"), "image_index": item.get("image_index")}
                      for item in items if item.get("item_id")]}


def parse(message, allowed):
    """The decisions, keeping only items the board actually carried.

    An id outside the board is dropped rather than trusted, and an action this
    loop does not know is refused rather than stored — a stored action is what
    the caller reads to decide what to do next.
    """
    calls = message.get("tool_calls") or []
    call = next((item for item in calls
                 if (item.get("function") or {}).get("name") == "submit_review"), None)
    if call is None:
        return []
    try:
        arguments = json.loads((call.get("function") or {}).get("arguments") or "{}")
    except (TypeError, ValueError):
        return []
    entries = arguments.get("items")
    if not isinstance(entries, list) or len(entries) > MAX_ITEMS:
        return []
    kept, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identity = str(entry.get("item_id"))
        action = str(entry.get("action") or "")
        if identity not in allowed or identity in seen or action not in ACTIONS:
            continue
        seen.add(identity)
        effort = str(entry.get("effort") or "")
        reason, hint = entry.get("reason"), entry.get("retry_hint")
        kept.append({"item_id": identity, "action": action,
                     "effort": effort if effort in EFFORTS else EFFORT_LATER,
                     "reason": reason[:200] if isinstance(reason, str) else "",
                     "retry_hint": hint[:120] if isinstance(hint, str) else ""})
    return kept


async def review(store, items, *, images=None, requester=None, timeout=90, session="review"):
    """Rule on one bounded board, with optional images attached to some items.

    ``images`` may be None. Two domains use this loop: one reviews pictures the
    sources already proved are of a known person, and one reviews records and
    names that have no picture at all. The shape is the same either way, which is
    the point — a second copy of this logic per domain is how the retry policies
    would start to disagree.

    Any failure returns ``[]``. A reviewer that cannot answer leaves the caller's
    state exactly as it was, which is the only safe outcome for a step that runs
    on a timer.
    """
    config = review_config(store)
    if config is None or not items:
        return []
    chosen = items[:MAX_ITEMS]
    allowed = {str(item.get("item_id")) for item in chosen}
    messages = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": json.dumps(board(chosen), ensure_ascii=False)}]
    if images:
        messages[1]["_images"] = list(images)
    duration = max(1.0, min(float(timeout), 180.0))
    if isinstance(timeout, bool) or not math.isfinite(duration):
        duration = 90.0
    request = requester or _default_requester
    try:
        message = await request(config, messages, duration, session)
    except Exception:
        return []
    return parse(message, allowed)


async def _default_requester(config, messages, duration, session):
    config = {**config, "_session_id": session}
    return await request_completion(config, messages, tools=[_tool()],
                                    tool_choice={"type": "function", "function": {"name": "submit_review"}},
                                    timeout=duration)
