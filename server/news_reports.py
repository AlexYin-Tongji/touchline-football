"""Durable, bounded editorial jobs; HTTP reads never invoke a model.

Source documents remain immutable evidence. Reports are separate derived records
keyed by current source versions and the reader's visibility/preferences. Only a
fully supported structured Agent delivery can become a completed report.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import threading
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from .agent_tools import LocalTools, parse_time
from .provider import AgentFailure, endpoint_url
from .research import article_url
from .team_profiles import _evidence_hash

MAX_DAILY_ATTEMPTS = 12
MAX_ATTEMPTS = 3
MAX_SOURCE_DOCUMENTS = 5
_WORKER_LOCK = threading.Lock()
# Remove only parenthesized machine-key scalar annotations. The surrounding
# Chinese qualification and all source/factual wording remain unchanged.
_READER_METADATA = re.compile(
    r"[（(]\s*(?:(?:content_status|content_method|text_truncated|spoiler_redacted|next_offset|end_offset|offset)"
    r"\s*[:=：]\s*[\"']?[A-Za-z0-9_.-]+[\"']?\s*(?:[,，;；]\s*)?)+[）)]")


def reading_report(report):
    """Presentation cleanup only; keep the verified run as the unchanged audit."""
    result = copy.deepcopy(report)
    def clean(value):
        return _READER_METADATA.sub("", value) if isinstance(value, str) else value
    for key in ("title", "lead", "analysis"):
        if key in result:
            result[key] = clean(result[key])
    result["next_steps"] = [clean(value) for value in result.get("next_steps", [])]
    for section in result.get("sections", []):
        for key in ("heading", "body"):
            if key in section:
                section[key] = clean(section[key])
    return result


def now():
    return datetime.now(timezone.utc)


def _stamp():
    return now().isoformat()


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _atomic(store):
    return store.atomic() if hasattr(store, "atomic") else nullcontext()


def _configured(settings):
    try:
        endpoint_url(settings.get("llm", {}))
        return True
    except (AgentFailure, TypeError, ValueError):
        return False


def _boundary(settings):
    # Interests and writing style affect future editorial choices; they are not
    # access permissions and must not erase already verified Chinese articles.
    return {"spoiler_free": bool(settings.get("spoiler_free", True))}


def _legacy_boundary(settings):
    return {"spoiler_free": settings.get("spoiler_free", True),
            "personalization": settings.get("personalization", {}),
            "explanation_depth": settings.get("explanation_depth", "standard")}


def source_version_hash(source):
    return _hash({"content": _evidence_hash(source), "private": source.get("private"),
                  "visibility": source.get("visibility"), "deleted": source.get("deleted")})


def _snapshot(store, event, settings, *, legacy=False, legacy_boundary=False):
    scope = {"id": "report_visibility", "mode": event["mode"], "team_id": event["team_id"], "skill": "news_report"}
    local = LocalTools(store, scope, settings)
    latest, all_hashes = {}, {}
    # Fingerprint all linked rows, including currently hidden rows. Any source
    # deletion/correction/privacy change invalidates the derived old wording.
    for eid in dict.fromkeys(event.get("evidence_ids", [])):
        raw = store.get("evidence", eid)
        # Fixture feeds already have dedicated schedules and standings. Their
        # short structured rows cannot support a substantive news report.
        if raw and raw.get("fixture_record"):
            continue
        all_hashes[eid] = (_hash(raw) if legacy else source_version_hash(raw)) if raw else "deleted"
        if not raw or not (view := local.evidence_view(raw)) or not (str(view.get("text") or "").strip() or not legacy and view.get("body_hash")):
            continue
        if raw.get("deleted") or raw.get("private") is True or raw.get("visibility") in {"hidden", "deleted"}:
            continue
        try:
            key = article_url(view["url"]) if view.get("url") else eid
        except ValueError:
            key = eid
        previous = latest.get(key)
        if not previous or str(view.get("fetched_at") or "") >= str(previous.get("fetched_at") or ""):
            latest[key] = view
    sources = sorted(latest.values(), key=lambda row: (row.get("published_at") or "", row.get("fetched_at") or ""), reverse=True)[:MAX_SOURCE_DOCUMENTS]
    boundary = _legacy_boundary(settings) if legacy or legacy_boundary else _boundary(settings)
    key = _hash({"version": 1 if legacy or legacy_boundary else 2, "team_id": event["team_id"], "mode": event["mode"], "event_id": event["id"],
                 "sources": all_hashes, "visible": {row["id"]: _evidence_hash(row, legacy=legacy) for row in sources},
                 "boundary": boundary})
    return {"fingerprint": key, "sources": sources, "source_hashes": all_hashes,
            "boundary_hash": _hash(boundary)}


def register_legacy_source_proof(store, report_id, legacy_source):
    """Verify a caller-supplied old snapshot, retain only its hash mapping.

    This never scans backups or reimports an article. A caller must explicitly
    supply the old record from a read-only backup. Every proof is bound to a
    report, its unchanged event membership, and its currently visible source.
    """
    row = store.get("reports", report_id)
    event = store.get("events", (row or {}).get("event_id"))
    eid = legacy_source.get("id") if isinstance(legacy_source, dict) else None
    current = store.get("evidence", eid) if eid else None
    if not row or not event or not current or row.get("status") != "completed":
        raise ValueError("旧来源证明缺少仍保留的完整报告、事件或来源。")
    snapshot = _snapshot(store, event, store.settings())
    if (set(row.get("source_hashes", {})) != set(snapshot["source_hashes"])
            or eid not in {source["id"] for source in snapshot["sources"]}):
        raise ValueError("事件来源集合或可见范围已经变化，不能登记旧证明。")
    if _hash(legacy_source) != row.get("source_hashes", {}).get(eid):
        raise ValueError("旧来源快照与报告保存的原始哈希不一致。")
    if any(legacy_source.get(key) != current.get(key) for key in
           ("id", "mode", "team_id", "title", "url", "source_name", "published_at", "origin", "spoiler", "private", "visibility", "deleted")):
        raise ValueError("当前来源元数据或权限与旧记录不一致。")
    legacy_body = str(legacy_source.get("text") or "")
    current_body_hash = current.get("body_hash") or hashlib.sha256(str(current.get("text") or "").encode()).hexdigest()
    if not legacy_body or hashlib.sha256(legacy_body.encode()).hexdigest() != current_body_hash:
        raise ValueError("当前来源正文版本与旧快照不一致。")
    proofs = store.get_meta("legacy_source_provenance_v1", {})
    proofs[eid] = {"legacy_record_hash": _hash(legacy_source), "stable_record_hash": source_version_hash(current)}
    store.set_meta("legacy_source_provenance_v1", proofs)
    return {"registered": True, "evidence_id": eid, "report_id": report_id}


def migrate_report_boundaries(store):
    """V2 only relaxes writing preferences, never source or visibility checks."""
    proofs = store.get_meta("legacy_source_provenance_v1", {})
    settings = store.settings()
    preserved = 0
    for row in store.list("reports"):
        if row.get("schema_version") == 2:
            continue
        reason, snapshot = None, None
        event = store.get("events", row.get("event_id"))
        if row.get("status") == "stale":
            reason = "previously_stale"  # Do not infer a past invalidation away.
        elif not event or (event.get("team_id"), event.get("mode")) != (row.get("team_id"), row.get("mode")):
            reason = "event_unavailable"
        else:
            snapshot = _snapshot(store, event, settings)
            if set(row.get("source_hashes", {})) != set(snapshot["source_hashes"]):
                reason = "source_membership_changed"
            elif not snapshot["sources"] or set(row.get("evidence_ids", [])) != {source["id"] for source in snapshot["sources"]}:
                reason = "source_visibility_changed"
            else:
                for eid, old_hash in row.get("source_hashes", {}).items():
                    current_hash = snapshot["source_hashes"][eid]
                    proof = proofs.get(eid, {})
                    if old_hash != current_hash and not (proof.get("legacy_record_hash") == old_hash and proof.get("stable_record_hash") == current_hash):
                        reason = "source_changed_or_provenance_unavailable"
                        break
        run = store.get("runs", row.get("run_id")) if row.get("run_id") else None
        if not reason and row.get("status") == "completed":
            if (not run or run.get("status") != "completed" or run.get("skill") != "news_report"
                    or (run.get("team_id"), run.get("mode")) != (row.get("team_id"), row.get("mode"))
                    or run.get("report_id") != row["id"] or run.get("delivery_downgraded")
                    or run.get("verification", {}).get("status") != "supported" or not row.get("report")):
                reason = "verified_delivery_unavailable"
            else:
                facts = (run.get("structured_answer") or {}).get("facts", [])
                cited = {eid for fact in facts for eid in fact.get("evidence_ids", [])}
                allowed = set(run.get("evidence_ids", [])) & set(run.get("read_evidence_ids", [])) & set(row.get("evidence_ids", []))
                sections = row["report"].get("sections", [])
                if not facts or not cited or not cited.issubset(allowed) or len(sections) != len(facts):
                    reason = "read_citations_unavailable"
                elif settings.get("spoiler_free", True) and run.get("spoiler"):
                    reason = "spoiler_scope_changed"
                elif any(row["report"].get(target, "") != _READER_METADATA.sub("", str(run.get("structured_answer", {}).get(origin, "")))
                         for target, origin in (("title", "title"), ("lead", "summary"), ("analysis", "analysis"))):
                    reason = "delivered_content_changed"
                elif any(set(section.get("evidence_ids", [])) != set(fact.get("evidence_ids", []))
                         or section.get("heading") != _READER_METADATA.sub("", fact.get("title", ""))
                         or section.get("body") != _READER_METADATA.sub("", fact.get("detail", ""))
                         for section, fact in zip(sections, facts)):
                    reason = "delivered_content_changed"
        elif not reason:
            # Pending jobs are not revalidated as old verified output. They
            # can be safely replaced by the normal queue on its next pass.
            reason = "not_completed"
        audit = {"version": 2, "result": "preserved" if not reason else "unchanged", "reason": reason or "same_sources_and_verified_delivery"}
        if not reason:
            row.update({key: snapshot[key] for key in ("fingerprint", "source_hashes", "boundary_hash")})
            row["schema_version"] = 2
            preserved += 1
        if row.get("boundary_migration") != audit:
            row["boundary_migration"] = audit
            store.put("reports", row)
    return {"preserved": preserved}


def _source_fields(snapshot):
    sources = snapshot["sources"]
    first = sources[0] if sources else {}
    title = str(first.get("title") or "")
    excerpt = ""
    return {"source_title": title, "source_excerpt": excerpt,
            "source_language": "zh" if len(re.findall(r"[\u3400-\u9fff]", excerpt)) > len(re.findall(r"[A-Za-z]", excerpt)) else "original",
            "source_count": len(sources), "evidence_ids": [item["id"] for item in sources]}


def _current(store, row, settings):
    event = store.get("events", row.get("event_id"))
    if not event or (event.get("team_id"), event.get("mode")) != (row.get("team_id"), row.get("mode")):
        return None
    snapshot = _snapshot(store, event, settings)
    return snapshot if snapshot["sources"] and snapshot["fingerprint"] == row.get("fingerprint") else None


def report_budget(store):
    today = now().date()
    used = sum(1 for row in store.list("reports") for value in row.get("attempt_history", [])
               if (stamp := parse_time(value)) and stamp.astimezone(timezone.utc).date() == today)
    resets = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    return {"daily_limit": MAX_DAILY_ATTEMPTS, "used_today": used,
            "remaining_today": max(0, MAX_DAILY_ATTEMPTS - used), "resets_at": resets.isoformat(), "concurrency": 1}


def _public(store, row, settings, budget):
    output = copy.deepcopy(row)
    snapshot = _current(store, row, settings)
    if not snapshot:
        event = store.get("events", row.get("event_id"))
        fresh = _snapshot(store, event, settings) if event and event.get("team_id") == row.get("team_id") and event.get("mode") == row.get("mode") else {"sources": []}
        output.update(_source_fields(fresh), status="stale", report=None,
                      error="原文或阅读范围已变化，旧报告已失效，需根据当前资料重新整理。")
    elif output.get("status") != "completed":
        output["report"] = None
    elif output.get("report"):
        output["report"] = reading_report(output["report"])
        # Reapply image visibility on every read, including reports created by
        # earlier versions. Safe text excerpts do not make an original image
        # safe: a photograph or scoreboard may reveal the removed result.
        cited = {source.get("id") for source in output["report"].get("sources", [])}
        allowed = {source["id"]: {image.get("url") for image in source.get("images", [])}
                   for source in snapshot["sources"] if source["id"] in cited and not source.get("spoiler_redacted")}
        from .ingestion import safe_image_url
        def images_in_scope(images, evidence_ids=None):
            return [image for image in (images or []) if isinstance(image, dict)
                    and isinstance(image.get("evidence_id"), str) and isinstance(image.get("url"), str)
                    and image.get("evidence_id") in allowed
                    and (evidence_ids is None or image["evidence_id"] in evidence_ids)
                    and image.get("url") in allowed[image["evidence_id"]]
                    and safe_image_url(image["url"], image.get("source_url") if isinstance(image.get("source_url"), str) else "")]
        output["report"]["images"] = images_in_scope(output["report"].get("images", []))
        for section in output["report"].get("sections", []):
            section["illustrations"] = images_in_scope(section.get("illustrations", []), section.get("evidence_ids", []))
    output.pop("images", None)
    output.pop("illustrations", None)
    output["budget"] = budget
    output["queue_note"] = ("今日自动整理额度已用完，将在额度重置后继续。" if output.get("status") == "queued" and not budget["remaining_today"]
                            else "等待后台整理" if output.get("status") == "queued" else None)
    # Internal version maps need not be sent to a browser.
    for key in ("source_hashes", "fingerprint", "boundary_hash", "attempt_history", "manual_requested", "schema_version", "boundary_migration"):
        output.pop(key, None)
    return output


def list_reports(store, team_id=None, mode="live"):
    settings, budget = store.settings(), report_budget(store)
    rows = store.list("reports", mode=mode, team_id=team_id)
    latest = {}
    for row in rows:
        key = (row.get("team_id"), row.get("event_id"))
        if key not in latest or row.get("updated_at", "") >= latest[key].get("updated_at", ""):
            latest[key] = row
    return [_public(store, row, settings, budget) for row in sorted(latest.values(), key=lambda row: row.get("created_at", ""), reverse=True)]


def get_report(store, report_id):
    row = store.get("reports", report_id)
    return _public(store, row, store.settings(), report_budget(store)) if row else None


def enqueue_reports(store, *, team_id=None, mode="live", event_ids=None, limit=30, force=False):
    """Persist missing jobs only; callers own scheduling after ingestion/import."""
    settings = store.settings()
    if mode != "live" or (not force and settings.get("reports", {}).get("auto_generate", True) is not True):
        return []
    events = store.list("events", mode=mode, team_id=team_id)
    if event_ids is not None:
        events = [event for event in events if event["id"] in set(event_ids)]
    events.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    result, existing_rows, queued = [], [], 0
    limit = max(0, min(100, int(limit)))
    for event in events:
        if queued >= limit:
            break
        snapshot = _snapshot(store, event, settings)
        if not snapshot["sources"]:
            continue
        report_id = "report_" + snapshot["fingerprint"][:28]
        with _atomic(store):
            existing = store.get("reports", report_id) or next((item for item in store.list("reports", mode=mode, team_id=event["team_id"])
                       if item.get("event_id") == event["id"] and item.get("fingerprint") == snapshot["fingerprint"]), None)
            if existing:
                if existing["status"] == "stale":
                    existing.update(status="queued" if _configured(settings) else "needs_configuration",
                                    attempts=0, report=None, error=None, retry_at=None, updated_at=_stamp())
                    store.put("reports", existing)
                    queued += 1
                    result.append(existing)
                elif existing["status"] == "needs_configuration" and _configured(settings):
                    existing.update(status="queued", error=None, updated_at=_stamp())
                    store.put("reports", existing)
                    queued += 1
                    result.append(existing)
                else:
                    existing_rows.append(existing)
                continue
            for previous in store.list("reports", mode=mode, team_id=event["team_id"]):
                if previous.get("event_id") == event["id"] and previous.get("status") != "stale":
                    previous.update(status="stale", report=None, updated_at=_stamp())
                    store.put("reports", previous)
            created = _stamp()
            row = {"id": report_id, "event_id": event["id"], "team_id": event["team_id"], "mode": mode,
                   "schema_version": 2,
                   "status": "queued" if _configured(settings) else "needs_configuration",
                   "error": None if _configured(settings) else "请先在设置中填写模型服务地址、模型名称和所需密钥，连接成功后自动整理。",
                   "created_at": created, "updated_at": created, "attempts": 0, "attempt_history": [],
                   "report": None, "run_id": None, "retry_at": None, "manual_requested": force,
                   "fingerprint": snapshot["fingerprint"], "source_hashes": snapshot["source_hashes"],
                   "boundary_hash": snapshot["boundary_hash"], **_source_fields(snapshot)}
            store.put("reports", row)
            result.append(row)
            queued += 1
    # Existing jobs do not consume the backfill batch. Keep their return value
    # for idempotent callers, while prioritizing newly queued work in the result.
    return [*result, *existing_rows][:limit]


def retry_report(store, report_id):
    row = store.get("reports", report_id)
    if not row:
        return None
    if not _current(store, row, store.settings()):
        replacements = enqueue_reports(store, team_id=row["team_id"], mode=row["mode"], event_ids=[row["event_id"]], force=True)
        return get_report(store, replacements[0]["id"]) if replacements else get_report(store, report_id)
    if row["status"] in {"completed", "generating", "queued"}:
        return get_report(store, report_id)
    row.update(status="queued" if _configured(store.settings()) else "needs_configuration", attempts=0,
               retry_at=None, error=None, updated_at=_stamp(), manual_requested=True)
    store.put("reports", row)
    return get_report(store, report_id)


def _finish(store, row, run, settings):
    snapshot = _current(store, row, settings)
    if not snapshot:
        row.update(status="stale", report=None, error="整理期间原文或阅读范围已变化，报告未发布。", updated_at=_stamp())
    elif (run.get("status") == "completed" and run.get("verification", {}).get("status") == "supported"
          and not run.get("delivery_downgraded") and run.get("structured_answer", {}).get("status") == "complete"):
        content = run["structured_answer"]
        facts = content.get("facts", [])
        cited = set(run.get("evidence_ids", []))
        read = set(run.get("read_evidence_ids", []))
        valid = bool(content.get("title") and 1 <= len(facts) <= 6 and cited and cited.issubset(read & set(row["evidence_ids"])))
        if not valid:
            return _fail(store, row, "报告结构或阅读引用不完整，未发布。")
        sources = [{key: item.get(key) for key in ("id", "title", "source_name", "url", "published_at")}
                   for item in snapshot["sources"] if item["id"] in cited]
        length = len(content.get("summary", "")) + sum(len(fact["detail"]) for fact in facts) + len(content.get("analysis", ""))
        illustrations = [{**image, "evidence_id": item["id"]} for item in snapshot["sources"] if item["id"] in cited
                         for image in item.get("images", [])[:2] if not item.get("spoiler_redacted")][:4]
        report = {"title": content["title"], "lead": content["summary"], "images": illustrations,
                  "sections": [{"heading": fact["title"], "body": fact["detail"], "kind": fact.get("kind", "development"), "evidence_ids": fact["evidence_ids"],
                                "illustrations": [image for image in illustrations if image["evidence_id"] in fact["evidence_ids"]][:1] if index else []} for index, fact in enumerate(facts)],
                  "analysis": content.get("analysis", ""), "next_steps": content.get("unknowns", []), "sources": sources,
                  "source_document_count": len({item.get("url") or item["id"] for item in sources}),
                  "reading_minutes": max(1, math.ceil(length / 400))}
        row.update(status="completed", report=reading_report(report), error=None, completed_at=_stamp(), updated_at=_stamp(),
                   verification_status="supported", retry_at=None)
    else:
        return _fail(store, row, run.get("error") or "现有原文不足以完成通过核对的中文报告，请补充正文或重试。")
    store.put("reports", row)
    return row


def _fail(store, row, error):
    row.update(status="failed", report=None, error=error, updated_at=_stamp(),
               retry_at=(now() + timedelta(minutes=5 * 2 ** max(0, row.get("attempts", 1) - 1))).isoformat() if row.get("attempts", 0) < MAX_ATTEMPTS else None)
    store.put("reports", row)
    return row


def recover_reports(store):
    """Startup-only recovery. Completed durable runs are adopted without billing."""
    recovered = 0
    for row in store.list("reports"):
        if row.get("status") != "generating":
            continue
        run = store.get("runs", row.get("run_id")) or {}
        if run.get("status") == "completed":
            _finish(store, row, run, store.settings())
        else:
            if run and run.get("status") in {"running", "queued"}:
                run.update(status="cancelled", error="服务重启，报告整理任务将从当前原文恢复。", finished_at=_stamp())
                store.put("runs", run)
            _fail(store, row, "上次整理被服务重启中断，已安排恢复。")
            row = store.get("reports", row["id"])
            if row.get("retry_at"):
                row.update(status="queued", retry_at=None)
                store.put("reports", row)
        recovered += 1
    return recovered


async def process_report_queue(store, *, max_jobs=None, runner=None):
    """One serial worker, <=3 jobs/cycle, <=12 billed attempts/UTC day."""
    if not _WORKER_LOCK.acquire(blocking=False):
        return {"processed": 0, "completed": 0, "failed": 0, "busy": True}
    stats = {"processed": 0, "completed": 0, "failed": 0, "busy": False}
    try:
        settings = store.settings()
        limit = max(0, min(3, int(max_jobs if max_jobs is not None else settings.get("reports", {}).get("max_per_cycle", 3))))
        queue = sorted(store.list("reports", mode="live"), key=lambda row: row.get("created_at", ""))
        for row in queue:
            if stats["processed"] >= limit:
                break
            if row.get("status") not in {"queued", "needs_configuration", "failed"}:
                continue
            settings = store.settings()
            if not settings.get("reports", {}).get("auto_generate", True) and not row.get("manual_requested"):
                continue
            if row.get("status") == "failed" and (not row.get("retry_at") or parse_time(row["retry_at"]) > now()):
                continue
            if not _current(store, row, settings):
                row.update(status="stale", report=None, updated_at=_stamp())
                store.put("reports", row)
                continue
            if not _configured(settings):
                row.update(status="needs_configuration", error="模型尚未配置，完成设置后将自动整理。", updated_at=_stamp())
                store.put("reports", row)
                continue
            if report_budget(store)["remaining_today"] <= 0:
                break
            run_id = "run_" + uuid.uuid4().hex[:20]
            row.update(status="generating", run_id=run_id, attempts=row.get("attempts", 0) + 1, error=None,
                       updated_at=_stamp(), started_at=_stamp(), retry_at=None,
                       attempt_history=[*row.get("attempt_history", []), _stamp()])
            store.put("reports", row)
            run = {"id": run_id, "team_id": row["team_id"], "mode": row["mode"], "skill": "news_report",
                   "conversation_id": "conversation_" + row["id"], "internal_report": True,
                   "report_id": row["id"], "event_id": row["event_id"], "input_evidence_ids": row["evidence_ids"],
                   "question": "把本次事件整理成可直接阅读的中文动态。标题具体，导语交代进展；通常1至3节、全文300至650字，交代原文细节与必要影响。有原文支持的背景才写背景，不凑篇幅。只用实际读过的原文，不拼凑其他事件，不编造事实，不重复空泛警示。",
                   "allow_network": False, "status": "queued", "created_at": _stamp(), "answer": "", "steps": [],
                   "evidence_ids": [], "warnings": [], "error": None, "finished_at": None, "model": ""}
            store.put("runs", run)
            stats["processed"] += 1
            try:
                if runner is None:
                    from .agent import run_agent
                    executor = run_agent
                else:
                    executor = runner
                await executor(store, run_id, asyncio.Event())
                # The Agent saves a cancelled run before returning. Preserve
                # the worker's shutdown request instead of starting another job.
                if asyncio.current_task() and asyncio.current_task().cancelling():
                    raise asyncio.CancelledError()
                # Re-read persisted job: a privacy/source mutation may have
                # invalidated it while the model request was in flight.
                row = store.get("reports", row["id"]) or row
                final = _finish(store, row, store.get("runs", run_id) or {}, store.settings())
            except asyncio.CancelledError:
                _fail(store, row, "整理被服务停止中断，下次启动会恢复。")
                raise
            except Exception:
                final = _fail(store, row, "后台整理未完成，已安排有限次数重试。")
            stats["completed" if final.get("status") == "completed" else "failed"] += 1
        return {**stats, "budget": report_budget(store)}
    finally:
        _WORKER_LOCK.release()
