"""Explicit, team-scoped single-pass work over the shared agent runtime."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import time
import uuid

from .agent_delegation import runtime_for
from .task_memory import state_key, prompt_context


def now():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Work:
    kind: str
    team_id: str
    revision: int
    stop: asyncio.Event
    task: asyncio.Task | None = None
    cycle_cancel: asyncio.Event | None = None
    root_id: str | None = None
    parts: list[str] | None = None


class WorkflowManager:
    KINDS = {"news", "team"}

    def __init__(self, store, *, cycle_runner=None):
        self.store = store
        self.tasks = {}
        self.stops = {}
        self.cycle_runner = cycle_runner
        self.team_slots = asyncio.Semaphore(2)

    def status(self, kind, team_id):
        if kind not in self.KINDS or not self.store.get("teams", team_id):
            raise ValueError("功能或球队不存在。")
        default = {"kind": kind, "team_id": team_id, "enabled": False, "status": "idle", "phase": "idle",
                   "message": "点击启动，更新一次资料。", "last_error": None, "started_at": None,
                   "finished_at": None, "updated_at": None, "next_run_at": None, "revision": 0,
                   "progress": {"completed": 0, "failed": 0, "remaining": 0},
                   "memory": {"summary": "尚未启动。", "next_steps": [], "completed_count": 0}}
        return copy.deepcopy({**default, **self.store.get_meta(state_key(kind, team_id), {})})

    def update(self, entry, **values):
        state = self.status(entry.kind, entry.team_id)
        if state["revision"] != entry.revision:
            return state
        if "progress" in values:
            delta = max(0, values["progress"]["completed"] - state["progress"]["completed"])
            if delta:
                state["memory"]["completed_count"] += delta
        state.update(values, updated_at=now())
        self.store.set_meta(state_key(entry.kind, entry.team_id), state)
        return state

    def recover(self):
        for team in self.store.list("teams"):
            for kind in self.KINDS:
                state = self.status(kind, team["id"])
                if state["enabled"] or state["status"] in {"running", "waiting", "stopping"}:
                    state.update(enabled=False, status="interrupted", phase="idle", next_run_at=None,
                                 message="上次运行已中断，已有成果保留；点击启动继续。", updated_at=now())
                    self.store.set_meta(state_key(kind, team["id"]), state)

    def start(self, kind, team_id, *, parts=None):
        state = self.status(kind, team_id)
        if parts is not None:
            from .team_center import _validated_parts
            if kind != "team":
                raise ValueError("只有球队资料任务接受更新范围。")
            parts = list(_validated_parts(parts))
        key = (kind, team_id)
        existing = self.tasks.get(key)
        if key in self.stops and not self.stops[key].done():
            return {**state, "started": False}
        if existing and existing.task and not existing.task.done():
            return {**state, "started": False}
        if kind == "news" and self.cycle_runner is None:
            from .news_reports import _configured, _report_settings
            if not _configured(_report_settings(self.store)):
                state.update(status="blocked", enabled=False, message="请先在设置中选择球队动态模型，再启动。",
                             last_error="球队动态模型尚未配置。", updated_at=now())
                self.store.set_meta(state_key(kind, team_id), state)
                return {**state, "started": False}
        entry = Work(kind, team_id, state["revision"] + 1, asyncio.Event(), parts=parts)
        state.update(revision=entry.revision, enabled=True, status="running", phase="starting",
                     message="正在接续已保存的工作。", started_at=now(), finished_at=None,
                     next_run_at=None, last_error=None, updated_at=now(),
                     progress={"completed": 0, "failed": 0, "remaining": 0})
        self.store.set_meta(state_key(kind, team_id), state)
        self.tasks[key] = entry
        entry.task = asyncio.create_task(self._run_once(entry))
        return {**state, "started": True}

    async def stop(self, kind, team_id, *, interrupted=False):
        self.status(kind, team_id)
        key = (kind, team_id)
        pending = self.stops.get(key)
        if pending is None or pending.done():
            pending = asyncio.create_task(self._stop(kind, team_id, interrupted=interrupted))
            self.stops[key] = pending
        return await asyncio.shield(pending)

    async def _stop(self, kind, team_id, *, interrupted=False):
        state = self.status(kind, team_id)
        entry = self.tasks.get((kind, team_id))
        if entry and entry.task and not entry.task.done():
            self.update(entry, enabled=False, status="stopping", phase="stopping", next_run_at=None,
                        message="正在结束本次工作，已完成内容会保留。")
            entry.stop.set()
            if entry.cycle_cancel:
                entry.cycle_cancel.set()
            if not entry.task.cancelling():
                entry.task.cancel()
            await asyncio.gather(entry.task, return_exceptions=True)
        state = self.status(kind, team_id)
        state.update(enabled=False, status="interrupted" if interrupted else "stopped", phase="idle",
                     next_run_at=None, finished_at=now(), updated_at=now(),
                     message="运行已中断，已有成果保留；点击启动继续。" if interrupted else "已结束，已完成内容已保留。")
        state["memory"] = {**state["memory"], "summary": state["message"], "next_steps": ["下次启动时接续未完成工作。"]}
        self.store.set_meta(state_key(kind, team_id), state)
        return state

    async def close(self):
        await asyncio.gather(*(self.stop(kind, team, interrupted=True) for kind, team in list(self.tasks)), return_exceptions=True)
        await asyncio.gather(*self.stops.values(), return_exceptions=True)

    async def _run_once(self, entry):
        runtime = runtime_for(self.store)
        entry.cycle_cancel = asyncio.Event()
        entry.root_id = "workflow_" + uuid.uuid4().hex
        runtime.register_root(entry.root_id, cancel_event=entry.cycle_cancel, deadline=time.monotonic() + 1800)
        outcome = "completed"
        try:
            try:
                if self.cycle_runner:
                    await self.cycle_runner(self, entry, True)
                elif entry.kind == "news":
                    await self._news_cycle(entry, True)
                else:
                    await self._team_cycle(entry, True)
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except Exception:
                self.update(entry, last_error="本次部分工作未完成，已有内容已保留；点击启动可重试。")
            state = self.status(entry.kind, entry.team_id)
            progress = state["progress"]
            incomplete = bool(state["last_error"] or progress["failed"] or progress["remaining"])
            outcome = ("partial" if progress["completed"] else "failed") if incomplete else "completed"
            note = (f"本次完成 {progress['completed']} 项，部分工作未完成，可手动重试。" if outcome == "partial"
                    else "本次未能完成，点击启动可重试。" if outcome == "failed"
                    else f"本次更新已完成，共完成 {progress['completed']} 项。")
            memory = {**state["memory"], "summary": note,
                      "next_steps": ["点击启动，接续未完成工作。"] if incomplete else []}
            self.update(entry, enabled=False, status=outcome, phase="idle", message=note, memory=memory,
                        next_run_at=None, finished_at=now())
        finally:
            try:
                runtime_status = "cancelled" if entry.stop.is_set() else "completed" if outcome == "partial" else outcome
                await runtime.finish_root(entry.root_id, runtime_status)
            finally:
                state = self.status(entry.kind, entry.team_id)
                if state["enabled"] and not entry.stop.is_set():
                    self.update(entry, enabled=False, status="interrupted", phase="idle", next_run_at=None,
                                finished_at=now(), message="运行已中断，已完成内容保留；点击启动继续。")
                self.tasks.pop((entry.kind, entry.team_id), None)

    async def _unit(self, entry, execute, payload):
        runtime = runtime_for(self.store)
        child = runtime.spawn(entry.root_id, execute=execute, payload=payload)
        cursor = 0
        try:
            while True:
                result = await runtime.wait(entry.root_id, [child["id"]], after=cursor, timeout=60)
                cursor = result["cursor"]
                current = result["agents"][0]
                if current["status"] in runtime.TERMINAL:
                    if current["status"] == "cancelled":
                        if entry.stop.is_set():
                            raise asyncio.CancelledError()
                        raise RuntimeError("本轮执行已中断")
                    if current["status"] != "completed":
                        raise RuntimeError("执行未完成")
                    return current.get("result") or {}
        finally:
            if entry.root_id in {row["id"] for row in runtime.snapshot()["active"]}:
                await runtime.cancel(entry.root_id, child["id"])

    async def _news_cycle(self, entry, first):
        from . import news_reports, research
        from .agent import run_agent
        def progress(value):
            current = {"completed": base["completed"] + value["completed"], "failed": base["failed"] + value["failed"],
                       "remaining": news_reports.queue_summary(self.store, team_id=entry.team_id)["total_pending"]}
            self.update(entry, phase="organizing", message="正在阅读来源、整理并核对新闻。", progress=current)
            if value.get("stage") == "finish" and value.get("run_id"):
                run = self.store.get("runs", value["run_id"]) or {}
                if run.get("status") == "failed":
                    error = run.get("error") or ""
                    note = ("部分稿件尚未通过原文核验，已保留来源和其他完成稿。" if "核对" in error or "核验" in error
                            else "模型服务暂未完成部分任务，可检查模型设置后重新启动。" if any(word in error for word in ("模型", "协议", "服务"))
                            else "部分消息的来源暂时不可读，已保留其他完成内容。")
                    self.update(entry, last_error=note)
        async def runner(store, run_id, cancel):
            run = store.get("runs", run_id)
            store.put("runs", {**run, "workflow_context": prompt_context(store, "news", entry.team_id)})
            async def execute(context):
                await run_agent(store, run_id, context.cancel_event, execution=context)
                return {"run_id": run_id}
            await self._unit(entry, execute, {"run_id": run_id})
        if first:
            news_reports.organize_reports(self.store, entry.team_id)
        base = self.status("news", entry.team_id)["progress"]
        await news_reports.process_report_queue(self.store, team_id=entry.team_id, owner_id=entry.root_id,
                                               cancel_event=entry.cycle_cancel, runner=runner, on_progress=progress)
        self.update(entry, phase="discovering", message="正在查找并读取新的消息来源。")
        async def collect(context):
            return await asyncio.wait_for(research.collect_team(self.store, entry.team_id, query="", limit=10, max_age_days=7, force=first), 180)
        found = await self._unit(entry, collect, {"team_id": entry.team_id})
        before = {row["id"] for row in self.store.list("reports", "live", entry.team_id)}
        queued = news_reports.enqueue_reports(self.store, team_id=entry.team_id)
        for row in queued:
            if row["id"] not in before:
                self.store.put("reports", {**row, "manual_once": True, "manual_stop_after_attempt": True})
        if found.get("errors"):
            self.update(entry, last_error="部分来源暂不可读，已保留成功获取的资料。")
        base = self.status("news", entry.team_id)["progress"]
        await news_reports.process_report_queue(self.store, team_id=entry.team_id, owner_id=entry.root_id,
                                               cancel_event=entry.cycle_cancel, runner=runner, on_progress=progress)

    async def _team_cycle(self, entry, first):
        from .team_center import maintain_team, center_view, sync_portraits
        self.update(entry, phase="maintaining", message="正在核对并更新阵容、赛程和积分。")
        async def maintain(context):
            async with self.team_slots:
                return await maintain_team(self.store, entry.team_id, force=first, **({"parts": entry.parts} if entry.parts else {}))
        outcome = await self._unit(entry, maintain, {"team_id": entry.team_id})
        components = outcome.get("components", {})
        selected = entry.parts or ("roster", "schedule", "standings")
        good = sum(components.get(p, {}).get("status") in {"completed", "fresh", "partial"} for p in selected)
        bad = sum(components.get(p, {}).get("status") in {"failed", "unavailable", "interrupted"} for p in selected)
        self.update(entry, progress={"completed": good, "failed": bad, "remaining": max(0, len(selected)-good)},
                    last_error="部分球队资料暂未更新，仍可查看上次有效资料。" if outcome.get("errors") else None)
        from .team_portraits import DIRECTORIES
        view = center_view(self.store, entry.team_id)
        if entry.parts is None and entry.team_id in DIRECTORIES and view.get("roster") and view.get("components", {}).get("portraits", {}).get("status") != "fresh":
            self.update(entry, phase="portraits", message="核心资料已保存，正在补充可核对的球员照片。")
            async def portraits(context):
                return await asyncio.wait_for(sync_portraits(self.store, entry.team_id), 120)
            await self._unit(entry, portraits, {"team_id": entry.team_id})
