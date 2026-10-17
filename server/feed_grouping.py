"""Which cards are the same story.

The feed's unit is the event, and events are grouped when an article is first
stored. That grouping is a rule: one, two, or three tellings of one story can
only be recognised if a rule recognises them. Rules are good at the mechanical
cases (the same address, the same person plus a figure) and bad at the ones that
matter most — a club announcement, a French report of it, and an English
analysis, which share no words, no URL and often not even a person.

Choosing which cards describe one story is a judgement about meaning, so it is
the model's job. What the model may do here is deliberately tiny:

* it sees an id, a headline, a short lead, the source names and the date;
* it returns groups of ids, each with the id whose card should represent the
  story;
* it writes nothing. Every title, lead, image and source on the merged card is
  an existing card's own field, and every source of every merged card is kept.

Ids outside the board are dropped, an id may appear in only one group, and a
lead that is not a member of its own group is ignored. If the model fails, is
unavailable, or returns nothing usable, the caller keeps its rule-based
grouping. Nothing here can hide a card whose sources have no counterpart.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from .model_config import AgentFailure, effective_config, role_selection
from .provider import request_completion

TOOL_NAME = "submit_groups"
MAX_CARDS = 40
# A group of one is not a group; the model only needs to report merges.
MIN_GROUP = 2

_SYSTEM = (
    "你在整理一个足球新闻聚合页的卡片。用户看到的是「谁在什么时候说了什么」，"
    "来源和原文链接必须保留。\n\n"
    "输入是一批卡片：编号、标题、导语片段、来源名、时间。"
    "你要判断哪些卡片讲的是同一件事——同一场比赛、同一次发布会、同一份公告、"
    "同一笔谈判，即使标题措辞、语言和角度不同。\n\n"
    "只输出确实讲同一件事的分组，每组 2 条以上：\n"
    "- lead 选最能代表这件事的一条，通常是信息最完整、来源最可靠的那条；\n"
    "- members 列出该组全部编号，必须包含 lead；\n"
    "- 每张卡片最多出现在一个分组里；没有分组的卡片不要输出。\n\n"
    "判断标准是「读者会不会觉得这是同一条消息被说了两遍」："
    "- 同一个发布会或专访的不同角度、同一份公告的官方与媒体版本、同一场比赛的赛后反应，属于一件事；\n"
    "- 同一主题的后续报道如果带来了新的事实（新的数字、新的回应、新的进展），就不属于同一件事；\n"
    "- 同一个人续约和受伤、同一支球队的两笔不同转会，是不同的事。\n"
    "宁可少分组，也不要把两件事并成一件。"
)


def _tool():
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "提交同一件事的卡片分组。没有可合并的卡片就返回空数组。",
            "parameters": {
                "type": "object",
                "properties": {
                    "groups": {
                        "type": "array",
                        "maxItems": MAX_CARDS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "lead": {"type": "string", "description": "代表这条故事的卡片编号。"},
                                "members": {
                                    "type": "array", "minItems": MIN_GROUP,
                                    "items": {"type": "string"},
                                    "description": "这一组全部卡片编号，包含 lead。",
                                },
                                "reason": {"type": "string", "maxLength": 120,
                                           "description": "一句话说明为什么是同一件事。"},
                            },
                            "required": ["lead", "members"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["groups"],
                "additionalProperties": False,
            },
        },
    }


def board(items):
    """What the model may see. Named fields only, so it cannot be widened upstream."""
    return {"cards": [{"id": row.get("id"), "title": row.get("title"),
                       "lead": (row.get("summary") or "")[:220],
                       "sources": [source.get("source_name") for source in row.get("sources") or []][:6],
                       "published": row.get("published_at") or row.get("published_date"),
                       "topic": row.get("topic")}
                      for row in items[:MAX_CARDS]]}


def parse(message, allowed):
    """Groups of ids the board actually carried; anything else is discarded."""
    call = next((item for item in (message.get("tool_calls") or [])
                 if (item.get("function") or {}).get("name") == TOOL_NAME), None)
    if call is None:
        return []
    try:
        arguments = json.loads((call.get("function") or {}).get("arguments") or "{}")
    except (TypeError, ValueError):
        return []
    entries = arguments.get("groups")
    if not isinstance(entries, list):
        return []
    used, groups = set(), []
    for entry in entries[:MAX_CARDS]:
        if not isinstance(entry, dict):
            continue
        lead = str(entry.get("lead") or "")
        members = [str(value) for value in entry.get("members") or [] if isinstance(value, str)]
        if lead not in members or lead not in allowed:
            continue
        kept = [value for value in dict.fromkeys(members)
                if value in allowed and value not in used]
        if lead not in kept or len(kept) < MIN_GROUP:
            continue
        used.update(kept)
        groups.append({"lead": lead, "members": kept,
                       "reason": str(entry.get("reason") or "")[:120]})
    return groups


def fingerprint(items):
    """The card set this grouping was computed from.

    Regrouping on every page load would spend a model call per refresh of the
    browser tab. The set of (id, title) pairs changes only when a refresh adds,
    retitles or drops a card, and that is exactly when the old grouping stops
    being usable.
    """
    basis = sorted((str(row.get("id")), str(row.get("title"))) for row in items)
    return hashlib.sha256(json.dumps(basis, ensure_ascii=False).encode()).hexdigest()[:32]


def cached(store, team_id):
    row = store.get_meta("feed_groups_" + str(team_id)) or {}
    return row if isinstance(row, dict) else {}


def merge(items, groups):
    """Fold each group into its lead card. Nothing is dropped but the duplicate card."""
    by_id = {row.get("id"): row for row in items}
    folded, absorbed = {}, set()
    for group in groups:
        lead = by_id.get(group["lead"])
        if lead is None:
            continue
        sources = {}
        images = []
        for identity in group["members"]:
            row = by_id.get(identity)
            if row is None:
                continue
            absorbed.add(identity)
            for source in row.get("sources") or []:
                if source.get("id"):
                    sources[source["id"]] = source
            images.extend(row.get("images") or [])
        merged = {**lead}
        merged["sources"] = list(sources.values())
        merged["evidence_ids"] = [source["id"] for source in merged["sources"]]
        seen, unique = set(), []
        for image in images:
            key = image.get("url")
            if key and key not in seen and len(unique) < 4:
                seen.add(key)
                unique.append(image)
        merged["images"] = unique
        merged["source_gap_count"] = sum((by_id[identity].get("source_gap_count") or 0)
                                         for identity in group["members"] if identity in by_id)
        merged["grouped_by"] = "model"
        merged["group_size"] = len(group["members"])
        folded[group["lead"]] = merged
    result = []
    for row in items:
        if row.get("id") in absorbed and row.get("id") not in folded:
            continue
        result.append(folded.get(row.get("id"), row))
    return result


async def regroup(store, team_id, items, *, timeout=90):
    """Ask the model to group the current cards, and cache the answer.

    Returns True when a grouping was stored. Any failure returns False and
    leaves the previous cache alone, so the caller's rule-based grouping stays
    in force.
    """
    if len(items) < MIN_GROUP:
        return False
    digest = fingerprint(items)
    current = cached(store, team_id)
    if current.get("fingerprint") == digest:
        return False
    selected = role_selection(store)
    if not selected:
        return False
    try:
        config = effective_config(store, selected["provider_id"], selected["model"])
    except AgentFailure:
        return False
    if not config:
        return False
    allowed = {str(row.get("id")) for row in items if row.get("id")}
    messages = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": json.dumps(board(items), ensure_ascii=False)}]
    config = {**config, "_session_id": "feed-groups"}
    try:
        message = await asyncio.wait_for(request_completion(
            config, messages, tools=[_tool()],
            tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
            timeout=max(1.0, float(timeout))), timeout=max(1.0, float(timeout)) + 5)
    except asyncio.CancelledError:
        raise
    except Exception:
        # A grouping pass that fails changes nothing. The rule-based collapse
        # in the feed view stays in force, which is the outcome that keeps
        # every card and every source visible.
        return False
    groups = parse(message, allowed)
    store.set_meta("feed_groups_" + str(team_id),
                   {"fingerprint": digest, "groups": groups, "at": _stamp()})
    return True


def _stamp():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def apply_cached(store, team_id, items, *, raw=None):
    """The stored grouping for exactly this card set, if it exists.

    ``raw`` is the card list before any rule-based collapse. The grouping was
    computed from that list, so the fingerprint has to be taken from it;
    comparing against the already-collapsed cards would never match, and the
    model's answer would sit in the store unused.
    """
    row = cached(store, team_id)
    if not row or row.get("fingerprint") != fingerprint(raw if raw is not None else items):
        return items
    groups = [group for group in row.get("groups") or [] if isinstance(group, dict)]
    return merge(items, groups) if groups else items
