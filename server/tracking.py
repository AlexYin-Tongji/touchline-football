"""Persistent follow-up jobs; collection and analysis share the normal run lifecycle."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def is_due(item):
    """Compatibility for saved jobs; checks are always started manually."""
    return False


async def execute_tracking(store, run_id, cancel_event):
    from . import research
    from .ingestion import safe_evidence_view
    from .agent import run_agent

    run = store.get("runs", run_id)
    item = store.get("tracking", run.get("tracking_id")) if run else None
    if not item:
        return
    checked_at = now()
    run.update(status="running", steps=[{"label": "检查跟进主题", "status": "running",
        "detail": "读取原始来源并与上次资料对照", "at": checked_at}])
    store.put("runs", run)
    try:
        result = await asyncio.wait_for(research.collect_team(store, item["team_id"],
            query=item["query"], limit=4, event_id=item.get("event_id"), match_id=item.get("match_id")), timeout=150)
        if cancel_event.is_set():
            raise asyncio.CancelledError()
        current_ids = list(dict.fromkeys(result.get("evidence_ids", [])))
        visible = [eid for eid in current_ids if (e := store.get("evidence", eid))
            and e.get("team_id") == item["team_id"] and e.get("mode") == "live"
            and (not store.settings().get("spoiler_free", True) or safe_evidence_view(e) is not None)]
        previous = set(item.get("baseline_evidence_ids", []))
        fresh = [eid for eid in visible if eid not in previous]
        run = store.get("runs", run_id)
        run["steps"][0].update(status="completed", detail=f"本次可见资料 {len(visible)} 份，其中新资料 {len(fresh)} 份")
        run["collection"] = {"added": result.get("added", 0), "new_visible": len(fresh),
                             "errors": result.get("errors", [])[:6]}
        run["redacted_evidence_ids"] = [eid for eid in visible if store.get("evidence", eid).get("spoiler")]
        if not current_ids and result.get("errors"):
            raise ValueError("本次未能取得相关原文。" + "；".join(result["errors"][:2]))
        if not fresh and item.get("last_checked"):
            run.update(status="completed", model="本次未调用模型", answer=(
                "本次检查没有发现新的可见原文，与上次相比没有需要重复解读的资料。"
                "这是当前已检查来源的结果，不表示外界没有新消息。"),
                evidence_ids=visible, finished_at=now(), error=None)
            if result.get("errors"):
                run.setdefault("warnings", []).append("部分来源本次未能读取，不能确认这些来源是否有更新。")
        elif not visible:
            run.update(status="completed", model="本次未调用模型", answer=(
                "暂未找到与这个主题直接相关的可见资料。可以缩短查询为球员名、伤情或转会等关键词，"
                "或添加更合适的来源。若资料包含赛果，请检查避剧透设置。"),
                evidence_ids=[], finished_at=now(), error=None)
        else:
            run.update(status="queued", allow_network=False, resume_evidence_ids=visible,
                question=f"本次检查主题：{item['title']}。\n关注的问题：{item['query']}。\n"
                         f"本次已收集的相关原文编号：{', '.join(visible)}。\n"
                         f"其中自上次检查后新出现的编号：{', '.join(fresh)}。\n"
                         "请读取原文并比较变化。分清新来源内容与真正的新事实，不将重复转述当独立核实；"
                         "先简洁说明现在能确认到哪一步、相比之前发生了什么、接下来需要等待什么。")
            store.put("runs", run)
            await run_agent(store, run_id, cancel_event)
            run = store.get("runs", run_id)
        store.put("runs", run)
        current = store.get("tracking", item["id"])
        if not current:
            return
        current.update(last_checked=checked_at, next_check=None,
                       last_run_id=run_id, last_error=run.get("error"))
        if run["status"] == "completed":
            current["status"] = "changed" if fresh else "unchanged"
            current["summary"] = run.get("answer", "")[:1600]
            current["baseline_evidence_ids"] = list(dict.fromkeys([*previous, *visible]))[-500:]
            if fresh:
                current.setdefault("updates", []).append({"at": now(), "run_id": run_id,
                    "evidence_ids": fresh, "query": item["query"], "summary": run.get("answer", "")[:1200]})
                current["updates"] = current["updates"][-50:]
        else:
            current["status"] = "failed" if run["status"] == "failed" else "idle"
        if not current.get("enabled"):
            current["status"] = "paused"
        store.put("tracking", current)
    except asyncio.CancelledError:
        run = store.get("runs", run_id)
        if run:
            run.update(status="cancelled", error="检查已停止。", finished_at=now())
            store.put("runs", run)
        current = store.get("tracking", item["id"])
        if current:
            current.update(status="idle" if current.get("enabled") else "paused",
                           next_check=None, last_error="本次检查已停止。")
            store.put("tracking", current)
    except Exception as exc:
        # Research errors are locally curated; arbitrary transport messages remain private.
        detail = str(exc)[:500] if isinstance(exc, ValueError) else "跟进检查未完成，请重试或检查来源与模型配置。"
        secret = store.settings().get("llm", {}).get("api_key", "")
        if secret:
            detail = detail.replace(secret, "[密钥已隐藏]")
        run = store.get("runs", run_id)
        if run:
            run.update(status="failed", error=detail, finished_at=now())
            store.put("runs", run)
        current = store.get("tracking", item["id"])
        if current:
            current.update(status="failed" if current.get("enabled") else "paused",
                last_checked=checked_at, next_check=None, last_error=detail)
            store.put("tracking", current)
