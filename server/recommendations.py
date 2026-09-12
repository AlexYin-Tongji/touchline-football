"""Small source-backed recommendations; no LLM call is needed to rank actions."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from .ingestion import safe_evidence_view


def timestamp(value):
    try:
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    except (ValueError, TypeError):
        return None


def for_team(store, team_id, mode="live", limit=3):
    now = datetime.now(timezone.utc)
    settings = store.settings()
    team = store.get("teams", team_id) or {}
    protect = settings.get("spoiler_free", True)
    evidence = {item["id"]: (safe_evidence_view(item) if protect else item)
                for item in store.list("evidence", mode, team_id)}
    evidence = {key: value for key, value in evidence.items() if value is not None}
    events = store.list("events", mode, team_id)
    matches = store.list("matches", mode, team_id)
    runs = store.list("runs", mode, team_id)
    tracked = {item.get("event_id") for item in store.list("tracking", mode, team_id) if item.get("enabled")}
    proposals = []

    def add(kind, title, reason, key, *, evidence_ids=(), priority="normal", match_id=None, event_id=None, query="", expires=None):
        ids = [eid for eid in evidence_ids if eid in evidence]
        identity = hashlib.sha256(f"{mode}:{team_id}:{kind}:{key}".encode()).hexdigest()[:18]
        identifier = "rec_" + identity
        existing = store.get("recommendations", identifier)
        item = {"id": identifier, "mode": mode, "team_id": team_id, "kind": kind, "title": title,
            "reason": reason, "priority": priority, "evidence_ids": ids, "match_id": match_id,
            "event_id": event_id, "query": query, "created_at": existing.get("created_at") if existing else now.isoformat(),
            "expires_at": (expires or now + timedelta(days=3)).isoformat(),
            "dismissed": bool(existing and existing.get("dismissed"))}
        store.put("recommendations", item)
        if not item["dismissed"]:
            proposals.append(item)

    upcoming = sorted((item for item in matches if item.get("status") != "finished"
        and item.get("fixture_status") not in {"cancelled", "postponed", "live"}
        and item.get("kickoff_confirmed", True)
        and timestamp(item.get("kickoff")) and timestamp(item["kickoff"]) > now), key=lambda x: x["kickoff"])
    if upcoming:
        match = upcoming[0]
        kickoff = timestamp(match["kickoff"])
        if kickoff - now < timedelta(days=7):
            lineups = match.get("lineups") or {}
            title = "首发已公布，更新观赛重点" if lineups.get("status") == "confirmed" else f"准备对阵{match['opponent']}的赛前看点"
            latest_preview = max((timestamp(run.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)
                for run in runs if run.get("match_id") == match["id"] and run.get("skill") == "match_preview" and run.get("status") == "completed"), default=None)
            changed_at = timestamp(lineups.get("updated_at") or match.get("updated_at") or match.get("synced_at"))
            if not latest_preview or (changed_at and changed_at > latest_preview):
                add("preview", title, "依据已保存的比赛时间与球队资料，在开球前明确可观察的问题。",
                    match["id"] + str(changed_at), match_id=match["id"], evidence_ids=match.get("evidence_ids", []),
                    priority="high", query="为本场比赛准备两条有原文支持的观赛重点，区分已确认名单与推测。", expires=kickoff)

    recent_finished = sorted((item for item in matches if item.get("status") == "finished"
        and timestamp(item.get("kickoff")) and timedelta(0) < now - timestamp(item["kickoff"]) < timedelta(days=2)),
        key=lambda x: x["kickoff"], reverse=True)
    if recent_finished:
        match = recent_finished[0]
        if not any(run.get("match_id") == match["id"] and run.get("skill") == "match_review" and run.get("status") == "completed" for run in runs):
            add("review", "本场比赛已有赛后资料，可以回看赛前判断", "比赛状态已更新；主动打开复盘才会揭示赛果。",
                match["id"], match_id=match["id"], evidence_ids=match.get("evidence_ids", []),
                query="我选择查看本场赛后资料，请核对赛前观察，避免补造比赛过程。",
                expires=timestamp(match["kickoff"]) + timedelta(days=3))

    visible_events = [item for item in events if item.get("evidence_ids") and all(eid in evidence for eid in item["evidence_ids"])
                      and not all(evidence[eid].get("fixture_record") for eid in item["evidence_ids"])]
    visible_events.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    for event in visible_events[:8]:
        updated = timestamp(event.get("updated_at"))
        if updated and now - updated > timedelta(days=7):
            continue
        key = event["id"] + ":" + ",".join(sorted(event["evidence_ids"]))
        if event.get("kind") in {"injury", "transfer"} and event["id"] not in tracked:
            stage = str(event.get("stage", ""))
            if not any(word in stage for word in ("官宣完成", "已正式签约", "确认复出")):
                add("follow", "继续关注这条人员消息", "已有原文，但出场安排或转会进展仍需后续来源确认。", key,
                    evidence_ids=event["evidence_ids"], event_id=event["id"], query=event["title"])
                break

    checked_questions = set()
    for run in sorted(runs, key=lambda row: row.get("created_at", ""), reverse=True)[:20]:
        if protect and (run.get("spoiler") or run.get("skill") == "match_review"):
            continue
        verification = run.get("verification") or {}
        question_key = (str(run.get("question", "")).strip(), run.get("match_id"))
        if question_key in checked_questions:
            continue
        if verification.get("status") in {"supported", "partial", "insufficient", "unavailable"}:
            checked_questions.add(question_key)
        if verification.get("status") in {"partial", "insufficient", "unavailable"}:
            add("verify", "这次解读还有证据缺口", "关键说法尚未全部获得原文支持，适合继续查证。", run["id"],
                evidence_ids=run.get("evidence_ids", []), query="请重新核对这个问题的关键说法并补充原文证据：" + run.get("question", ""), priority="high")
            break

    if len(proposals) < limit:
        for event in visible_events:
            if event.get("read"):
                continue
            updated = timestamp(event.get("updated_at"))
            if updated and now - updated > timedelta(days=7):
                continue
            add("read", "读一条刚更新的原始消息", "这条事件还有未读原文，先核对出处再形成判断。",
                event["id"] + str(event.get("updated_at")), evidence_ids=event["evidence_ids"], event_id=event["id"],
                query=event["title"])
            break
    if mode == "live" and not evidence:
        add("read", f"收集{team.get('name', '球队')}的公开消息", "当前还没有可用原文，可从球队官网与公开订阅开始。",
            "empty", query="球队最新消息", priority="high")
    if mode == "live" and not (settings["llm"].get("model") and settings["llm"].get("base_url")):
        add("setup", "连接模型以获取有依据的解读", "资料收集现在即可使用；模型连接后可进一步核对与解释。", "model", priority="high")
    proposals.sort(key=lambda item: item["priority"] != "high")
    return proposals[:limit]


def get_recommendations(store, mode="live"):
    result = []
    for team_id in store.settings().get("followed_teams", []):
        result.extend(for_team(store, team_id, mode, 3))
    return result
