"""Read-only Chinese event cards backed by delivered, unchanged source records."""
from __future__ import annotations

import copy
import re
from urllib.parse import urlsplit

from .agent_tools import clean_delivery_text
from .events import ENTITY_ALIASES, entities
from .research import article_categories, article_url
from .team_profiles import _evidence_hash, knowledge_for_team


def _normalized(value):
    return " ".join(str(value or "").split())


def readable_chinese(value):
    value = _normalized(value)
    chinese = len(re.findall(r"[\u3400-\u9fff]", value))
    latin = len(re.findall(r"[A-Za-z]", value))
    return bool(chinese >= 2 and latin <= max(20, chinese)
                and not re.search(r"(?:[A-Za-z]+[\s,;:]+){5}[A-Za-z]+", value)
                and clean_delivery_text(value))


def source_label(source, team_name="球队"):
    host = (urlsplit(source.get("url") or "").hostname or "").lower()
    for domain, label in {"arsenal.com": "阿森纳官网", "liverpoolfc.com": "利物浦官网",
                          "fcbarcelona.com": "巴塞罗那官网", "inter.it": "国际米兰官网",
                          "theguardian.com": "卫报体育", "bbc.co.uk": "英国广播公司体育", "bbc.com": "英国广播公司体育"}.items():
        if host == domain or host.endswith("." + domain):
            return label
    name = source.get("source_name") or ""
    return name if readable_chinese(name) else team_name + "相关资讯"


def _short_excerpt(value, limit=110):
    clean = re.sub(r"\*\*|【[^】]*】", "", _normalized(value)).strip()
    sentence = re.split(r"(?<=[。！？])", clean)[0]
    return sentence if len(sentence) <= limit else sentence[:limit].rstrip("，、； ") + "…"


def _fallback(event, records, team_name):
    first = records[-1] if records else {}
    category = {"injury": "伤停动态", "transfer": "转会动态", "match": "比赛动态"}.get(event.get("kind"))
    if not category:
        labels = {"injury": "伤停动态", "training": "训练动态", "transfer": "转会动态", "squad": "阵容动态",
                  "preview": "赛前动态", "review": "赛后动态", "tactics": "战术动态", "interview": "采访动态", "discipline": "纪律动态"}
        topics = article_categories({"title": event.get("title"), "text": event.get("summary")})
        category = next((labels[key] for key in topics if key in labels), "球队动态")
    people = [next((alias for alias in ENTITY_ALIASES[key] if re.search(r"[\u3400-\u9fff]", alias)), "")
              for key in entities(event.get("title", ""))[:2]]
    subject = "、".join(filter(None, people)) or source_label(first, team_name)
    title, summary = event.get("title") or "", event.get("summary") or ""
    if readable_chinese(title) and readable_chinese(summary):
        title, summary = _short_excerpt(title, 56), _short_excerpt(summary)
        if _normalized(title).rstrip("。") != _normalized(summary).rstrip("。"):
            return {"display_title": title, "display_summary": summary, "summary_origin": "source",
                    "display_status": "unverified", "source_run_id": None}
    return {"display_title": _short_excerpt(title, 56) if readable_chinese(title) else f"{subject} · {category}",
            "display_summary": "相关报道已收录，可查看来源，或请助手整理中文要点。",
            "summary_origin": "pending", "display_status": "pending", "source_run_id": None}


def _run_fact(run, title, detail, ids):
    delivered = set(run.get("evidence_ids") or [])
    read_ids = set(run.get("read_evidence_ids") or [])
    if not ids or not ids.issubset(delivered & read_ids):
        return None
    for fact in (run.get("structured_answer") or {}).get("facts", []):
        if (isinstance(fact, dict) and _normalized(fact.get("title")) == _normalized(title)
                and _normalized(fact.get("detail")) == _normalized(detail)
                and set(fact.get("evidence_ids") or []) == ids):
            claim_text = _normalized(title + "：" + detail)
            claims = [claim for claim in (run.get("verification") or {}).get("claims", [])
                      if str(claim.get("id", "")).startswith("fact_") and _normalized(claim.get("text")) == claim_text]
            if any(claim.get("verdict") in {"unsupported", "conflicting", "uncertain"} for claim in claims):
                return None
            return "supported" if any(claim.get("verdict") == "supported" for claim in claims) else "unverified"
    return None


def _current_source_ids(records):
    """An immutable old row may still be superseded by a newer page capture."""
    latest = {}
    for record in records:
        url = record.get("url")
        if not url:
            continue
        try:
            key = article_url(url)
        except ValueError:
            continue
        previous = latest.get(key)
        if not previous or (record.get("fetched_at") or "") > (previous.get("fetched_at") or ""):
            latest[key] = record
    current = {record["id"] for record in records}
    for record in records:
        try:
            newer = latest.get(article_url(record["url"])) if record.get("url") else None
        except ValueError:
            newer = None
        if newer and newer["id"] != record["id"] and _evidence_hash(newer) != _evidence_hash(record):
            current.discard(record["id"])
    return current


def present_events(store, events, evidence, runs, *, spoiler_free=True):
    """Add display fields without rewriting stored source text or event history.

    Old run IDs alone cannot prove unchanged source versions. A knowledge hash
    or explicit recorded source hash is required before surfacing its wording.
    """
    visible = {item["id"]: item for item in evidence}
    run_map = {run["id"]: run for run in runs if run.get("status") == "completed" and not run.get("spoiler_hidden")
               and (not spoiler_free or not run.get("spoiler") and run.get("skill") != "match_review")}
    knowledge_by_scope = {}
    result = []
    for event in events:
        scope = (event.get("team_id"), event.get("mode"))
        ids = {eid for eid in event.get("evidence_ids", []) if eid in visible
               and (visible[eid].get("team_id"), visible[eid].get("mode")) == scope}
        records = [visible[eid] for eid in event.get("evidence_ids", []) if eid in ids]
        current_ids = _current_source_ids(records)
        team = store.get("teams", scope[0]) or {}
        card = _fallback(event, records, team.get("name", "球队"))
        if scope not in knowledge_by_scope:
            knowledge_by_scope[scope] = knowledge_for_team(store, *scope, spoiler_free=spoiler_free,
                                                         allowed_evidence_ids=set(visible), limit=None)
        choices = []
        for item in knowledge_by_scope[scope]:
            fact_ids = set(item.get("evidence_ids") or [])
            if not fact_ids or not fact_ids.issubset(current_ids) or not all(readable_chinese(item.get(key)) for key in ("title", "detail")):
                continue
            for run_id in item.get("run_ids", []):
                run = run_map.get(run_id)
                if not run or (run.get("team_id"), run.get("mode")) != scope:
                    continue
                status = _run_fact(run, item["title"], item["detail"], fact_ids)
                if status:
                    # The archive's weaker provenance must never be promoted.
                    if item.get("support") != "source_supported":
                        status = "unverified"
                    choices.append((status == "supported", run.get("finished_at") or "", item, status, run_id))
        # Support explicit immutable snapshots if a caller persisted them; an
        # unversioned legacy run falls through to the honest source placeholder.
        for run in run_map.values():
            if (run.get("team_id"), run.get("mode")) != scope:
                continue
            hashes = run.get("evidence_hashes") or {}
            if not hashes:
                continue
            for fact in (run.get("structured_answer") or {}).get("facts", []):
                if not isinstance(fact, dict):
                    continue
                fact_ids = set(fact.get("evidence_ids") or [])
                if not fact_ids or not fact_ids.issubset(current_ids) or not all(readable_chinese(fact.get(key)) for key in ("title", "detail")):
                    continue
                raw_sources = [store.get("evidence", eid) for eid in fact_ids]
                if any(not source or hashes.get(source["id"]) != _evidence_hash(source) for source in raw_sources):
                    continue
                status = _run_fact(run, fact["title"], fact["detail"], fact_ids)
                if status:
                    choices.append((status == "supported", run.get("finished_at") or "", fact, status, run["id"]))
        if choices:
            _, _, fact, status, run_id = max(choices, key=lambda choice: choice[:2])
            card = {"display_title": fact["title"], "display_summary": fact["detail"], "summary_origin": "assistant",
                    "display_status": status, "source_run_id": run_id}
        result.append({**copy.deepcopy(event), **card})
    return result
