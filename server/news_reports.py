"""Durable editorial jobs; HTTP reads never invoke a model.

Source documents remain immutable evidence. Reports are separate derived records
keyed by current source versions and the reader's visibility/preferences. Only a
source-linked structured Agent delivery can become a completed report. Key-fact
review is advisory to readers; incomplete semantic checks retain the article
with one visible notice, while source access and actual-read citations remain
mandatory.
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

REPORT_CONCURRENCY = 4
# Only identified transient failures are eligible for a later owned batch.
# Manual attempts remain single-pass; citation or content failures never loop.
FAILED_REPORT_ATTEMPT_LIMIT = 5


def FAILED_REPORT_BACKOFF_SECONDS(attempts):
    """Wait before retrying a failed report: 15m, 1h, 4h, 12h."""
    return (900, 3600, 14400, 43200)[min(int(attempts or 0), 3)]


def failure_kind(row):
    """Conservative retry classification; unknown failures need user action."""
    error = str(row.get("error") or "").lower()
    if any(term in error for term in ("配额", "余额", "额度不足", "insufficient_quota", "quota exhausted", "billing", "credit balance")):
        return "source_or_configuration"
    if (any(term in error for term in ("引用不完整", "报告结构", "原文不足", "确定性", "permanent fault"))
            or "未通过" in error and any(term in error for term in ("核对", "核验"))):
        return "content_review"
    if any(term in error for term in ("配置", "协议", "接口", "http 400", "http 401", "http 403", "http 404",
                                      "权限", "身份", "范围", "私有", "删除", "停用", "证书", "citation")):
        return "source_or_configuration"
    if any(term in error for term in ("已停止", "被停止", "运行中断", "服务重启", "时间限制", "预算", "过多工具", "cancelled")):
        return "execution_boundary"
    gaps = row.get("source_errors") or []
    if any(gap.get("code") in {"timeout", "dns_failed", "connection_error", "connection_reset", "http_rate_limited", "http_server_error"}
           or gap.get("http_status") in {429, 500, 502, 503, 504} for gap in gaps if isinstance(gap, dict)):
        return "transient"
    if any(term in error for term in ("temporary outage", "请求超时", "响应超时", "连接模型服务", "连接中断",
                                      "模型暂不可用", "模型服务暂不可用", "服务过载", "限流", "速率限制", "http 429",
                                      "http 500", "http 502", "http 503", "http 504", "connection reset")):
        return "transient"
    return "unknown"


def _elapsed(value):
    """Seconds since an ISO timestamp; None or unparseable counts as long ago."""
    try:
        return max(0.0, (datetime.now(timezone.utc) - parse_time(value)).total_seconds())
    except Exception:
        return float("inf")


def _worker_lock(store, team_id=None):
    if team_id is not None:
        locks = getattr(store, "_report_team_worker_locks", None)
        if locks is None:
            locks = store._report_team_worker_locks = {}
        return locks.setdefault(team_id, threading.Lock())
    lock = getattr(store, "_report_worker_lock", None)
    if lock is None:
        lock = threading.Lock()
        store._report_worker_lock = lock
    return lock


def _worker_slots(store):
    slots = getattr(store, "_report_executor_slots", None)
    if slots is None:
        slots = store._report_executor_slots = asyncio.Semaphore(REPORT_CONCURRENCY)
    return slots


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


def _report_settings(store):
    from .model_config import role_settings, verification_config
    try:
        settings = role_settings(store, "feed")
        settings["verification_llm"] = verification_config(store, settings.get("llm", {}))
        return settings
    except AgentFailure:
        return {**store.settings(), "llm": {}}


def _configuration_revision(settings):
    # Credential changes should resume exhausted jobs too. Only a one-way hash
    # is persisted, never a copy of the configured key or its public prefix.
    llm = settings.get("llm", {})
    fields = ("base_url", "protocol", "model", "api_key")
    draft = {key: llm.get(key) for key in fields}
    checker = {key: settings.get("verification_llm", llm).get(key) for key in fields}
    # Preserve the old identity when verification follows the task model.
    # Selecting/fixing a separate checker can resume failed automatic jobs.
    return _hash(draft if checker == draft else {"draft": draft, "verify": checker})


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


def _source_scope_hash(source):
    return _hash({key: source.get(key) for key in
                  ("id", "team_id", "mode", "url", "source_id", "origin", "private", "visibility", "deleted", "spoiler", "manual_spoiler")})


def adopt_initial_reads(store, run, local):
    """Bind a new draft to its actual first read, before any model wording.

    A restart expires transient bodies. Reading the same link may obtain a
    corrected version; it is valid input to a NEW draft, never to an old one.
    Membership, permissions and unread sources must still match the claim.
    """
    if not run.get("internal_report") or not run.get("report_source_scope_hashes"):
        return
    with _atomic(store):
        row = store.get("reports", run.get("report_id"))
        if not row or row.get("status") != "generating" or row.get("run_id") != run.get("id"):
            raise AgentFailure("本次整理任务已失效，未开始生成稿件。")
        event = store.get("events", row.get("event_id"))
        if not event or (event.get("team_id"), event.get("mode")) != (row.get("team_id"), row.get("mode")):
            raise AgentFailure("事件范围已变化，未开始生成稿件。")
        snapshot = _snapshot(store, event, store.settings())
        if (snapshot["boundary_hash"] != row.get("boundary_hash")
                or set(snapshot["source_hashes"]) != set(row.get("source_hashes", {}))
                or {source["id"] for source in snapshot["sources"]} != set(row.get("evidence_ids", []))):
            raise AgentFailure("来源集合或阅读范围已变化，未开始生成稿件。")
        for eid, original in row["source_hashes"].items():
            current = store.get("evidence", eid)
            if current is None and original == "deleted" and snapshot["source_hashes"][eid] == "deleted":
                continue
            if not current or _source_scope_hash(current) != run["report_source_scope_hashes"].get(eid):
                raise AgentFailure("来源权限或身份已变化，未开始生成稿件。")
            if snapshot["source_hashes"][eid] != original and (eid not in local.read_ids or not local.cache_is_current(eid)):
                raise AgentFailure("尚未读取的来源已变化，未开始生成稿件。")
        if snapshot["fingerprint"] != row["fingerprint"]:
            row.update(fingerprint=snapshot["fingerprint"], source_hashes=snapshot["source_hashes"],
                       **_source_fields(snapshot), initial_reads_at=_stamp(), updated_at=_stamp())
            store.put("reports", row)


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
    sources = sorted(latest.values(), key=lambda row: (row.get("published_at") or row.get("published_date") or "", row.get("fetched_at") or ""), reverse=True)
    boundary = _legacy_boundary(settings) if legacy or legacy_boundary else _boundary(settings)
    # Preserve the existing fingerprint format so removing the old retrieval
    # cap does not invalidate verified Chinese reports. All source versions and
    # permissions remain fingerprinted in all_hashes; the actual source list
    # returned below is complete and has no five-document retrieval limit.
    key = _hash({"version": 1 if legacy or legacy_boundary else 2, "team_id": event["team_id"], "mode": event["mode"], "event_id": event["id"],
                 "sources": all_hashes, "visible": {row["id"]: _evidence_hash(row, legacy=legacy) for row in sources[:5]},
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
    proof = {"legacy_record_hash": _hash(legacy_source), "stable_record_hash": source_version_hash(current)}
    if proofs.get(eid) != proof:
        # A newly supplied proof can change the next migration's outcome even
        # after a prior audit rejected this report. Preserve that earlier state.
        backup = getattr(store, "backup", None)
        if callable(backup):
            backup("before-provenance")
        proofs[eid] = proof
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
    """Usage audit only; report generation has no daily or per-click quota."""
    today = now().date()
    counts = {"auto": 0, "manual": 0}
    for row in store.list("reports"):
        for value in row.get("attempt_history", []):
            stamp = parse_time(value.get("at") if isinstance(value, dict) else value)
            if stamp and stamp.astimezone(timezone.utc).date() == today:
                kind = "manual" if isinstance(value, dict) and value.get("kind") == "manual" else "auto"
                counts[kind] += 1
    used = counts["auto"]
    return {"daily_limit": None, "used_today": used,
            "remaining_today": None, "resets_at": None, "concurrency": REPORT_CONCURRENCY,
            "manual_used_today": counts["manual"], "manual_batch_limit": None}


def _manual_active(row):
    return row.get("status") in {"queued", "generating"} and bool(row.get("manual_once") or row.get("manual_report"))


def _public(store, row, settings, budget):
    output = copy.deepcopy(row)
    snapshot = _current(store, row, settings)
    if not snapshot:
        event = store.get("events", row.get("event_id"))
        fresh = _snapshot(store, event, settings) if event and event.get("team_id") == row.get("team_id") and event.get("mode") == row.get("mode") else {"sources": []}
        output.update(_source_fields(fresh), status="stale", report=None,
                      source_errors=[], error="原文或阅读范围已变化，旧报告已失效，需根据当前资料重新整理。")
    elif output.get("status") != "completed":
        output["report"] = None
    elif output.get("report"):
        output["report"] = reading_report(output["report"])
        from .ingestion import source_diagnostics
        output["report"]["source_gaps"] = source_diagnostics(output["report"].get("source_gaps"), snapshot["sources"])
        # Reapply image visibility on every read, including reports created by
        # earlier versions. Safe text excerpts do not make an original image
        # safe: a photograph or scoreboard may reveal the removed result.
        cited = {source.get("id") for source in output["report"].get("sources", [])}
        from .personal_app import source_tier
        current_sources = {source["id"]: source for source in snapshot["sources"]}
        output["report"]["sources"] = [
            {**source, **{key: current_sources[source["id"]].get(key) for key in ("published_at", "published_date", "publication_precision")},
             **source_tier(current_sources[source["id"]].get("url") or source.get("url") or "")}
            for source in output["report"].get("sources", []) if source.get("id") in current_sources]
        from .ingestion import image_identity, source_images
        allowed = {source["id"]: {image["url"]: image for image in source_images(source)}
                   for source in snapshot["sources"] if source["id"] in cited}
        def images_in_scope(images, evidence_ids=None):
            result, seen = [], set()
            for image in images or []:
                if not isinstance(image, dict):
                    continue
                eid, url = image.get("evidence_id"), image.get("url")
                if not isinstance(eid, str) or not isinstance(url, str) or eid not in allowed:
                    continue
                current = allowed[eid].get(url)
                if not current or (evidence_ids is not None and eid not in evidence_ids) or image_identity(url) in seen:
                    continue
                # Use current attribution, so stale captions/links cannot leak.
                result.append(current)
                seen.add(image_identity(url))
            return result
        output["report"]["images"] = images_in_scope(output["report"].get("images", []))
        for section in output["report"].get("sections", []):
            section["illustrations"] = images_in_scope(section.get("illustrations", []), section.get("evidence_ids", []))
    output.pop("images", None)
    output.pop("illustrations", None)
    if snapshot:
        from .ingestion import source_diagnostics
        output["source_errors"] = source_diagnostics(output.get("source_errors"), snapshot["sources"])
    output["budget"] = budget
    output["retry_at"] = None  # Legacy timers no longer schedule another attempt.
    status = output.get("status")
    output["manual_organize"] = _manual_active(output)
    if status == "failed":
        output["failure_kind"] = failure_kind(output)
        output["automatic_retry_eligible"] = (output["failure_kind"] == "transient"
            and not output.get("manual_stop_after_attempt")
            and int(output.get("attempts") or 0) < FAILED_REPORT_ATTEMPT_LIMIT)
    output["can_refresh_retry"] = status == "failed" and not output.get("manual_stop_after_attempt")
    output["queue_note"] = ("已加入本次手动整理，后台将优先处理。" if status == "queued" and output["manual_organize"]
                            else "已加入整理队列，后台正在依次处理。" if status == "queued"
                            else "正在阅读来源并整理中文消息。" if status == "generating"
                            else "本次手动整理未完成，可点击手动整理重试。" if status == "failed" and output.get("manual_stop_after_attempt")
                            else "本次整理未完成，可查看失败原因后手动重试。" if status == "failed"
                            else "请在设置中为球队动态选择可用模型。" if status == "needs_configuration" else None)
    # Internal version maps need not be sent to a browser.
    for key in ("source_hashes", "fingerprint", "boundary_hash", "attempt_history", "manual_requested", "manual_once", "manual_report", "manual_stop_after_attempt", "schema_version", "boundary_migration", "config_revision"):
        output.pop(key, None)
    return output


def list_reports(store, team_id=None, mode="live"):
    settings, budget = store.settings(), report_budget(store)
    rows = store.list("reports", mode=mode, team_id=team_id)
    latest, priorities, versions = {}, {}, {}
    for row in rows:
        key = (row.get("team_id"), row.get("event_id"))
        if key not in versions:
            event = store.get("events", row.get("event_id"))
            snapshot = (_snapshot(store, event, settings) if event
                        and (event.get("team_id"), event.get("mode")) == (row.get("team_id"), row.get("mode")) else None)
            versions[key] = snapshot["fingerprint"] if snapshot and snapshot["sources"] else None
        # A superseded worker can finish after the current report. Its late
        # status update must never replace the report for today's sources.
        priority = (versions[key] is not None and row.get("fingerprint") == versions[key], row.get("updated_at", ""))
        if key not in latest or priority >= priorities[key]:
            latest[key] = row
            priorities[key] = priority
    return [_public(store, row, settings, budget) for row in sorted(latest.values(), key=lambda row: row.get("created_at", ""), reverse=True)]


def get_report(store, report_id):
    row = store.get("reports", report_id)
    return _public(store, row, store.settings(), report_budget(store)) if row else None


def queue_summary(store, *, team_id=None, reports=None, waiting_sources=0):
    """Read-only editorial state, with mutually exclusive waiting reasons."""
    reports = list_reports(store, team_id=team_id) if reports is None else reports
    result = dict.fromkeys(("queued", "generating", "retrying", "failed", "needs_configuration", "budget_blocked"), 0)
    result.update(waiting_sources=max(0, waiting_sources), next_retry_at=None, manual_count=0)
    for row in reports:
        status = row.get("status")
        if status == "completed":
            continue
        manual = row.get("manual_organize", _manual_active(row))
        if manual:
            result["manual_count"] += 1
        if status == "queued":
            result["queued"] += 1
        elif status in {"generating", "failed", "needs_configuration"}:
            result[status] += 1
        else:
            result["waiting_sources"] += 1
    result["total_pending"] = sum(result[key] for key in ("queued", "generating", "retrying", "failed", "needs_configuration", "budget_blocked", "waiting_sources"))
    messages = []
    for key, label in (("generating", "条正在整理"), ("queued", "条已排队"),
                       ("failed", "条暂未整理成功，可点击手动整理重试"), ("needs_configuration", "条等待配置球队动态模型"),
                       ("waiting_sources", "条正在等待可用来源与整理")):
        if result[key]:
            messages.append(str(result[key]) + label)
    result["note"] = "；".join(messages) or "中文动态已更新。"
    result["can_retry"] = bool(any(row.get("can_refresh_retry", row.get("status") == "failed" and not row.get("manual_stop_after_attempt"))
                                   for row in reports) or result["waiting_sources"])
    result["can_organize"] = bool(result["total_pending"] and not result["manual_count"] and _configured(_report_settings(store)))
    return result


def enqueue_reports(store, *, team_id=None, mode="live", event_ids=None, limit=None, force=False):
    """Persist missing jobs only; callers own scheduling after ingestion/import."""
    settings = _report_settings(store)
    if mode != "live" or (not force and settings.get("reports", {}).get("auto_generate", True) is not True):
        return []
    events = store.list("events", mode=mode, team_id=team_id)
    if event_ids is not None:
        events = [event for event in events if event["id"] in set(event_ids)]
    events.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    result, existing_rows, queued = [], [], 0
    limit = max(0, int(limit)) if limit is not None else None
    for event in events:
        if limit is not None and queued >= limit:
            break
        snapshot = _snapshot(store, event, settings)
        if not snapshot["sources"]:
            continue
        report_id = "report_" + snapshot["fingerprint"][:28]
        with _atomic(store):
            existing = store.get("reports", report_id) or next((item for item in store.list("reports", mode=mode, team_id=event["team_id"])
                       if item.get("event_id") == event["id"] and item.get("fingerprint") == snapshot["fingerprint"]), None)
            if existing:
                changed_config = existing.get("config_revision") != _configuration_revision(settings)
                if existing.get("manual_stop_after_attempt") and not existing.get("manual_once"):
                    existing_rows.append(existing)
                elif existing["status"] == "stale" or existing["status"] == "failed" and changed_config:
                    existing.update(status="queued" if _configured(settings) else "needs_configuration",
                                    attempts=0, report=None, error=None, retry_at=None, updated_at=_stamp(),
                                    config_revision=_configuration_revision(settings))
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
                   "error": None if _configured(settings) else "请先在设置中填写模型服务地址、模型名称和所需密钥，连接成功后手动启动整理。",
                   "created_at": created, "updated_at": created, "attempts": 0, "attempt_history": [],
                   "report": None, "run_id": None, "retry_at": None, "manual_requested": force,
                   "config_revision": _configuration_revision(settings),
                   "fingerprint": snapshot["fingerprint"], "source_hashes": snapshot["source_hashes"],
                   "boundary_hash": snapshot["boundary_hash"], **_source_fields(snapshot)}
            store.put("reports", row)
            result.append(row)
            queued += 1
    # Existing jobs do not consume the backfill batch. Keep their return value
    # for idempotent callers, while prioritizing newly queued work in the result.
    return [*result, *existing_rows][:limit]


def retry_report(store, report_id, *, explicit=False):
    row = store.get("reports", report_id)
    if not row:
        return None
    if not _current(store, row, store.settings()):
        replacements = enqueue_reports(store, team_id=row["team_id"], mode=row["mode"], event_ids=[row["event_id"]], force=True)
        if replacements and explicit:
            return retry_report(store, replacements[0]["id"], explicit=True)
        return get_report(store, replacements[0]["id"]) if replacements else get_report(store, report_id)
    if row["status"] in {"completed", "generating"} or row["status"] == "queued" and not explicit:
        return get_report(store, report_id)
    if row.get("manual_stop_after_attempt") and not explicit:
        return get_report(store, report_id)
    settings = _report_settings(store)
    row.update(status="queued" if _configured(settings) else "needs_configuration", attempts=0,
               retry_at=None, error=None, updated_at=_stamp(), manual_requested=True,
               config_revision=_configuration_revision(settings))
    if explicit:
        row.update(manual_once=True, manual_report=True, manual_stop_after_attempt=True)
    store.put("reports", row)
    return get_report(store, report_id)


def retry_pending_reports(store, team_id=None, limit=None):
    """Explicit refresh retries eligible failures, preserving attempt history."""
    teams = {team_id} if team_id else set(store.settings().get("followed_teams", []))
    ids = []
    for row in list_reports(store, team_id=team_id):
        if limit is not None and len(ids) >= max(0, limit):
            break
        if row.get("team_id") not in teams or row.get("status") not in {"failed", "stale", "needs_configuration"}:
            continue
        if (store.get("reports", row["id"]) or {}).get("manual_stop_after_attempt"):
            continue
        retried = retry_report(store, row["id"])
        if retried and retried.get("status") == "queued":
            ids.append(retried["id"])
    return {"queued": len(ids), "report_ids": ids}


def organize_reports(store, team_id, *, event_ids=None, retry_failed=True):
    """Authorize the current pending-source snapshot, with no item quota."""
    settings = _report_settings(store)
    if not _configured(settings):
        raise ValueError("请先在设置中为“球队动态”选择可用模型，再手动整理。")
    from .presentation import readable_chinese
    selected_events = set(event_ids) if event_ids is not None else None
    with _atomic(store):
        active = [row for row in store.list("reports", "live", team_id) if _manual_active(row)
                  and (selected_events is None or row.get("event_id") in selected_events)]
        if active:
            return {"started": False, "status": "busy", "queued": len(active),
                    "report_ids": [row["id"] for row in active if selected_events is None or row.get("event_id") in selected_events],
                    "note": "本队已有手动整理任务，请等待这一批完成。"}
        events = sorted(store.list("events", "live", team_id),
                        key=lambda row: row.get("updated_at") or row.get("created_at") or "", reverse=True)
        if selected_events is not None:
            events = [event for event in events if event["id"] in selected_events]
        selected, unchanged_failed = [], 0
        for event in events:
            snapshot = _snapshot(store, event, settings)
            if not snapshot["sources"]:
                continue
            # Already readable source material needs no model request.
            if any(readable_chinese(source.get("title", "")) and readable_chinese(source.get("text", ""))
                   for source in snapshot["sources"]):
                continue
            existing = next((row for row in store.list("reports", "live", team_id)
                             if row.get("event_id") == event["id"] and row.get("fingerprint") == snapshot["fingerprint"]), None)
            if existing and existing.get("status") in {"completed", "generating"}:
                continue
            if (existing and existing.get("status") == "failed" and not retry_failed
                    and existing.get("config_revision") == _configuration_revision(settings)):
                unchanged_failed += 1
                continue
            rows = enqueue_reports(store, team_id=team_id, event_ids=[event["id"]], limit=1, force=True)
            if not rows:
                continue
            row = rows[0]
            if not _current(store, row, settings):
                continue
            row.update(status="queued", manual_once=True, manual_report=True, manual_stop_after_attempt=True,
                       attempts=0, retry_at=None, error=None, updated_at=_stamp(),
                       config_revision=_configuration_revision(settings))
            store.put("reports", row)
            selected.append(row["id"])
        return {"started": bool(selected), "status": "queued" if selected else "idle", "queued": len(selected),
                "report_ids": selected, "unchanged_failed": unchanged_failed,
                "note": f"已安排手动整理 {len(selected)} 条消息，完成后会加入动态。" if selected
                        else "暂无可手动整理的消息。可以先刷新来源，再手动整理。"}


def _finish(store, row, run, settings):
    snapshot = _current(store, row, settings)
    from .ingestion import source_diagnostics
    row["source_errors"] = source_diagnostics(run.get("source_errors"), snapshot["sources"] if snapshot else [])
    if not snapshot:
        row.update(status="stale", report=None, error="整理期间原文或阅读范围已变化，报告未发布。", updated_at=_stamp())
    elif (run.get("status") == "completed" and (run.get("verification", {}).get("status") == "supported" or run.get("citation_integrity_checked") is True)
          and not run.get("delivery_downgraded") and run.get("structured_answer", {}).get("status") == "complete"):
        content = run["structured_answer"]
        facts = content.get("facts", [])
        cited = set(run.get("evidence_ids", []))
        read = set(run.get("read_evidence_ids", []))
        valid = bool(content.get("title") and facts and cited and cited.issubset(read & set(row["evidence_ids"])))
        if not valid:
            return _fail(store, row, "报告结构或阅读引用不完整，未发布。")
        sources = [{key: item.get(key) for key in ("id", "title", "source_name", "url", "published_at", "published_date", "publication_precision")}
                   for item in snapshot["sources"] if item["id"] in cited]
        length = len(content.get("summary", "")) + sum(len(fact["detail"]) for fact in facts) + len(content.get("analysis", ""))
        from .ingestion import image_identity, source_images
        illustrations, seen_images = [], set()
        for item in snapshot["sources"]:
            if item["id"] not in cited:
                continue
            for image in source_images(item, limit=2):
                identity = image_identity(image["url"])
                if identity not in seen_images and len(illustrations) < 4:
                    illustrations.append(image)
                    seen_images.add(identity)
        report = {"title": content["title"], "lead": content["summary"], "images": illustrations,
                  "sections": [{"heading": fact["title"], "body": fact["detail"], "kind": fact.get("kind", "development"), "evidence_ids": fact["evidence_ids"],
                                "illustrations": [image for image in illustrations if image["evidence_id"] in fact["evidence_ids"]][:1] if index else []} for index, fact in enumerate(facts)],
                  "analysis": content.get("analysis", ""), "next_steps": content.get("unknowns", []), "sources": sources,
                  "source_document_count": len({item.get("url") or item["id"] for item in sources}),
                  "reading_minutes": max(1, math.ceil(length / 400)),
                  "source_gaps": [gap for gap in row["source_errors"] if gap["evidence_id"] not in cited]}
        review = run.get("verification", {}).get("status")
        coverage = run.get("news_coverage", {}).get("status")
        if review == "supported" and coverage not in {"incomplete", "unavailable"} and not run.get("news_repair_incomplete"):
            review_status, review_notice = "checked", None
        elif review in {"unavailable", "not_requested", None}:
            review_status, review_notice = "unavailable", "本文已依据所附来源整理；重点信息核对暂未全部完成，关键细节请对照原文。"
        else:
            review_status, review_notice = "partial", "本文已保留完整稿件，部分重点信息或长文内容仍待进一步核对，请结合所附来源阅读。"
        report.update(review_status=review_status, review_notice=review_notice)
        row.update(status="completed", report=reading_report(report), error=None, completed_at=_stamp(), updated_at=_stamp(),
                   verification_status=review, retry_at=None)
    else:
        return _fail(store, row, run.get("error") or "现有原文不足以完成通过核对的中文报告，请补充正文或重试。")
    store.put("reports", row)
    return row


def _fail(store, row, error):
    row.update(status="failed", report=None, error=error, updated_at=_stamp(),
               retry_at=None)
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
                run.update(status="cancelled", error="服务重启，手动整理已中断，请重新手动整理。" if row.get("manual_stop_after_attempt")
                           else "服务重启，报告整理已中断，请手动启动继续。", finished_at=_stamp())
                store.put("runs", run)
            _fail(store, row, "上次手动整理被服务重启中断，请再次手动整理。" if row.get("manual_stop_after_attempt")
                  else "上次整理被服务重启中断，请手动启动继续。")
            row = store.get("reports", row["id"])
            if row.get("retry_at"):
                row.update(status="queued", retry_at=None)
                store.put("reports", row)
        recovered += 1
    return recovered


async def process_report_queue(store, *, max_jobs=None, runner=None, team_id=None,
                               owner_id=None, cancel_event=None, on_progress=None, report_ids=None):
    """Drain one owned batch; all scopes share the same per-store execution slots.

    A global batch and team batches may coexist. Their short synchronous claims
    recheck durable state only after obtaining a slot. Cancellation stops only
    the workers created by this invocation and waits for their cleanup.
    """
    limit = max(0, int(max_jobs)) if max_jobs is not None else None
    selected_reports = set(report_ids) if report_ids is not None else None
    stop = cancel_event if cancel_event is not None else asyncio.Event()
    stats = {"processed": 0, "completed": 0, "failed": 0, "busy": False}
    examined, active = set(), {}

    def progress(stage, run_id=None):
        if on_progress is not None:
            try:
                on_progress({**stats, "stage": stage, "run_id": run_id,
                             "active_run_ids": list(active), "owner_id": owner_id, "team_id": team_id})
            except (Exception, asyncio.CancelledError):
                # Progress is metadata only and must never change execution.
                pass

    if stop.is_set():
        progress("cancelled")
        raise asyncio.CancelledError()
    lock = _worker_lock(store, team_id)
    if not lock.acquire(blocking=False):
        stats["busy"] = True
        progress("busy")
        return stats
    slots = _worker_slots(store)

    def queue_rows():
        return sorted((row for row in store.list("reports", mode="live", team_id=team_id)
                       if row["id"] not in examined and (selected_reports is None or row["id"] in selected_reports)
                       and row.get("status") in {"queued", "needs_configuration", "failed"}),
                      key=lambda row: (not row.get("manual_once", False), not row.get("manual_requested", False),
                                       row.get("status") == "failed", row.get("created_at", "")))

    def claim():
        # No await occurs between the status check and the generating write.
        # Store.atomic also protects callers using the same store from threads.
        with _atomic(store):
            for row in queue_rows():
                examined.add(row["id"])
                manual = bool(row.get("manual_once"))
                if row.get("manual_stop_after_attempt") and not manual:
                    continue
                settings = _report_settings(store)
                if not settings.get("reports", {}).get("auto_generate", True) and not (manual or row.get("manual_requested")):
                    continue
                if row.get("status") == "failed":
                    if failure_kind(row) != "transient":
                        continue
                    attempts = int(row.get("attempts") or 0)
                    if attempts >= FAILED_REPORT_ATTEMPT_LIMIT:
                        continue
                    if _elapsed(row.get("updated_at")) < FAILED_REPORT_BACKOFF_SECONDS(attempts):
                        continue
                if not _current(store, row, settings):
                    row.update(status="stale", report=None, updated_at=_stamp(), manual_once=False)
                    store.put("reports", row)
                    continue
                if not _configured(settings):
                    row.update(status="needs_configuration", manual_once=False,
                               error="模型尚未配置，请在设置中选择球队动态模型。", updated_at=_stamp())
                    store.put("reports", row)
                    continue
                run_id = "run_" + uuid.uuid4().hex[:20]
                row.update(status="generating", run_id=run_id, attempts=row.get("attempts", 0) + 1, error=None, source_errors=[],
                           updated_at=_stamp(), started_at=_stamp(), retry_at=None,
                           manual_once=False, manual_report=manual,
                           config_revision=_configuration_revision(settings),
                           attempt_history=[*row.get("attempt_history", []), {"at": _stamp(), "kind": "manual" if manual else "auto"}])
                run = {"id": run_id, "team_id": row["team_id"], "mode": row["mode"], "skill": "news_report",
                       "conversation_id": "conversation_" + row["id"], "internal_report": True, "manual_report": manual,
                       "report_id": row["id"], "event_id": row["event_id"], "input_evidence_ids": row["evidence_ids"],
                       "question": "把本次事件整理成可直接阅读的中文动态。标题具体，导语交代这一件事和它的最新进展；材料若有多篇报道，先合并成一件事再写，重复叙述不要重复出现，各篇说法不一致时并列写清。根据事件复杂度和原文材料自行安排篇幅与章节。有原文支持的背景才写背景，不凑篇幅。只用实际读过的原文，不拼凑其他事件，不编造事实，不重复空泛警示。",
                       "allow_network": False, "status": "queued", "created_at": _stamp(), "answer": "", "steps": [],
                       "evidence_ids": [], "warnings": [], "error": None, "finished_at": None,
                       "model": settings.get("llm", {}).get("model", ""), "model_role": "feed"}
                run["report_source_scope_hashes"] = {eid: _source_scope_hash(store.get("evidence", eid) or {})
                                                     for eid in row.get("source_hashes", {})}
                if owner_id is not None:
                    run["workflow_id"] = owner_id
                from .model_config import role_selection
                selection = role_selection(store, "feed")
                if selection:
                    run.update(provider_id=selection["provider_id"], model=selection["model"])
                store.put("reports", row)
                store.put("runs", run)
                return row, run_id, manual
        return None

    def interrupted(row, manual):
        message = "手动整理被停止，请再次启动整理。" if manual else "整理已停止，下次启动会恢复。"
        current = store.get("reports", row["id"])
        run = store.get("runs", row.get("run_id"))
        if run and run.get("status") in {"queued", "running"}:
            store.put("runs", {**run, "status": "cancelled", "finished_at": _stamp(), "error": message})
        if not current or current.get("run_id") != row.get("run_id"):
            return None
        if current.get("status") != "generating":
            return current
        # An executor can durably finish just before the stop request arrives.
        # Adopt only an already verified result; never discard finished work.
        if run and run.get("status") == "completed":
            return _finish(store, current, run, store.settings())
        return _fail(store, current, message)

    async def worker():
        while limit is None or stats["processed"] < limit:
            if stop.is_set():
                raise asyncio.CancelledError()
            # This is only a cheap hint. Another scope may claim these rows
            # while this worker waits; claim() always queries again afterwards.
            if not queue_rows():
                return
            async with slots:
                if stop.is_set():
                    raise asyncio.CancelledError()
                if limit is not None and stats["processed"] >= limit:
                    return
                selected = claim()
                if selected is None:
                    return
                row, run_id, manual = selected
                active[run_id] = (row, manual)
                stats["processed"] += 1
                progress("claim", run_id)
                final, cancelled = None, False
                try:
                    if stop.is_set():
                        raise asyncio.CancelledError()
                    if runner is None:
                        from .agent import run_agent
                        executor = run_agent
                    else:
                        executor = runner
                    await executor(store, run_id, stop)
                    if stop.is_set() or (asyncio.current_task() and asyncio.current_task().cancelling()):
                        raise asyncio.CancelledError()
                    current = store.get("reports", row["id"])
                    if current and current.get("run_id") == run_id:
                        final = _finish(store, current, store.get("runs", run_id) or {}, store.settings())
                except asyncio.CancelledError:
                    cancelled = True
                    final = interrupted(row, manual)
                    raise
                except Exception:
                    current_run = store.get("runs", run_id)
                    if current_run and current_run.get("status") in {"queued", "running"}:
                        store.put("runs", {**current_run, "status": "failed", "finished_at": _stamp(),
                                           "error": "后台整理未完成，已保留来源和失败状态。"})
                    current = store.get("reports", row["id"])
                    if current and current.get("run_id") == run_id:
                        final = current if current.get("status") == "stale" else _fail(store, current,
                            "本次手动整理未完成，请再次手动整理。")
                finally:
                    active.pop(run_id, None)
                    if final is not None:
                        stats["completed" if final.get("status") == "completed" else "failed"] += 1
                    progress("cancelled" if cancelled else "finish", run_id)

    workers, watcher, completion = [], None, None
    cancelled = False
    try:
        workers = [asyncio.create_task(worker()) for _ in range(REPORT_CONCURRENCY)]
        completion = asyncio.gather(*workers)
        watcher = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({completion, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if watcher in done or stop.is_set():
            raise asyncio.CancelledError()
        await completion
        return {**stats, "budget": report_budget(store)}
    except asyncio.CancelledError:
        cancelled = True
        stop.set()
        raise
    finally:
        async def cleanup():
            try:
                for task in [*workers, *([watcher] if watcher is not None else [])]:
                    if not task.done() and not task.cancelling():
                        task.cancel()
                await asyncio.gather(*workers, *([watcher] if watcher is not None else []), return_exceptions=True)
                if completion is not None:
                    await asyncio.gather(completion, return_exceptions=True)
                for row, manual in list(active.values()):
                    interrupted(row, manual)
                if cancelled:
                    progress("cancelled")
            finally:
                lock.release()
        cleaning = asyncio.create_task(cleanup())
        # Repeated cancellation must not interrupt a runner's finally cleanup
        # or release the batch lock before its report executions have stopped.
        while not cleaning.done():
            try:
                await asyncio.shield(cleaning)
            except asyncio.CancelledError:
                cancelled = True
                stop.set()
        await cleaning
        if cancelled:
            raise asyncio.CancelledError()
