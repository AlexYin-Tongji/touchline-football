"""Read-only Chinese event cards backed by delivered, unchanged source records."""
from __future__ import annotations

import copy
import re

from .agent_tools import clean_delivery_text
from .research import article_url
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


def _short_excerpt(value, limit=110):
    clean = re.sub(r"\*\*|【[^】]*】", "", _normalized(value)).strip()
    sentence = re.split(r"(?<=[。！？])", clean)[0]
    return sentence if len(sentence) <= limit else sentence[:limit].rstrip("，、； ") + "…"


def _fallback(event, records):
    first = records[-1] if records else {}
    title, summary = event.get("title") or "", event.get("summary") or ""
    if records and readable_chinese(title) and readable_chinese(summary):
        title, summary = _short_excerpt(title, 56), _short_excerpt(summary)
        if _normalized(title).rstrip("。") != _normalized(summary).rstrip("。"):
            return {"display_title": title, "display_summary": summary, "summary_origin": "source",
                    "display_status": "unverified", "source_run_id": None}
    # Preserve actual language until the editorial job succeeds. A category
    # label and an empty invitation to ask again are not a translated report.
    source_title = str((first.get("title") or title) if records else "暂无可读来源")
    source_text = str((first.get("text") or summary) if records else "")
    return {"display_title": _short_excerpt(source_title, 150),
            "display_summary": _short_excerpt(source_text, 360),
            "summary_origin": "pending", "display_status": "pending", "source_run_id": None,
            "display_language": "zh" if readable_chinese(source_text) else "original",
            "report_note": "中文报告待整理" if records else "当前阅读范围内暂无可读原文"}


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
    from .news_reports import list_reports
    report_map = {(item.get("team_id"), item.get("mode"), item.get("event_id")): item
                  for mode in {event.get("mode") for event in events}
                  for item in list_reports(store, mode=mode)}
    result = []
    for event in events:
        scope = (event.get("team_id"), event.get("mode"))
        ids = {eid for eid in event.get("evidence_ids", []) if eid in visible
               and (visible[eid].get("team_id"), visible[eid].get("mode")) == scope}
        records = [visible[eid] for eid in event.get("evidence_ids", []) if eid in ids]
        current_ids = _current_source_ids(records)
        card = _fallback(event, records)
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
        editorial = report_map.get((*scope, event.get("id")))
        if editorial:
            card.update(report_id=editorial["id"], report_status=editorial["status"],
                        report_note=editorial.get("error") or editorial.get("queue_note"))
            report = editorial.get("report")
            if editorial["status"] == "completed" and report:
                card.update(display_title=report["title"], display_summary=report["lead"],
                            summary_origin="report", display_status="supported", source_run_id=editorial.get("run_id"),
                            reading_minutes=report.get("reading_minutes"), section_count=len(report.get("sections", [])))
        images = [{**image, "evidence_id": record["id"]} for record in records if not record.get("spoiler_redacted")
                  for image in record.get("images", [])[:2]][:4]
        sources = [{key: record.get(key) for key in ("id", "title", "source_name", "url", "published_at")} for record in records]
        result.append({**copy.deepcopy(event), **card, "images": images, "sources": sources})
    return result
