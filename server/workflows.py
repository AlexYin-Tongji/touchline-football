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


def collection_summary(result):
    """Describe discovery separately from the paid editorial work."""
    values = result.get("counts") or result
    sources = result.get("sources") or []
    def count(key, fallback=0):
        fallback = max(0, fallback) if type(fallback) is int else 0
        value = values.get(key)
        if value is None:
            observed = [source[key] for source in sources if type(source.get(key)) is int]
            value = sum(observed) if observed else fallback
        return max(0, value) if type(value) is int else fallback
    available = sum(source.get("state") not in {"unavailable", "failed"} for source in sources)
    readable = count("readable_count", len(result.get("evidence_ids") or []))
    attempted = count("read_attempted", result.get("attempted_reads", 0))
    candidates = result.get("candidates") or []
    relevant = count("relevant_count", len(candidates))
    indexed = count("indexed_count", relevant)
    dated = count("dated_count", sum(bool(row.get("published_at")) for row in candidates))
    window = count("window_count", len(candidates))
    date_pending = count("date_pending_count")
    date_unknown = count("date_unknown_count")
    rejected = count("scope_rejected_count")
    date_rejected = count("date_scope_rejected_count")
    errors = bool(result.get("errors"))
    if readable:
        status, reason = ("partial" if errors else "ready"), "readable"
    elif (attempted and rejected >= attempted) or (relevant and date_rejected >= relevant):
        status, reason = "empty", "out_of_scope"
    elif attempted:
        status, reason = "failed", "unreadable"
    elif (sources and not available) or (not sources and errors):
        status, reason = "failed", "sources_unavailable"
    elif date_pending and not window:
        status, reason = "partial", "date_pending"
    elif (relevant and not dated) or (date_unknown and not window):
        status, reason = "partial", "date_unknown"
    else:
        status, reason = "empty", "outside_window" if relevant else "no_matches"
    return {"status": status, "reason": reason, "indexed_count": indexed,
            "relevant_count": relevant, "dated_count": dated, "window_count": window,
            "date_pending_count": date_pending, "date_unknown_count": date_unknown,
            "scope_rejected_count": rejected, "date_scope_rejected_count": date_rejected,
            "read_attempted": attempted, "readable_count": readable,
            "source_count": len(sources), "available_sources": available,
            "failed_sources": sum(source.get("state") in {"unavailable", "failed"} for source in sources)}


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
    report_ids: tuple[str, ...] | None = None


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
                   "source_errors": [],
                   "collection": None,
                   "finished_at": None, "updated_at": None, "next_run_at": None, "revision": 0,
                   "progress": {"completed": 0, "failed": 0, "remaining": 0},
                   "memory": {"summary": "尚未启动。", "next_steps": [], "completed_count": 0}}
        state = copy.deepcopy({**default, **self.store.get_meta(state_key(kind, team_id), {})})
        if kind == "team":
            state.setdefault("leaves", [])
            state.setdefault("actionable_count", None)
            state.setdefault("panel", None)
            progress = dict(state.get("progress") or {})
            progress.setdefault("completed", 0)
            progress.setdefault("failed", 0)
            progress.setdefault("remaining", 0)
            progress.setdefault("settled", 0)
            state["progress"] = progress
        return state

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

    def start(self, kind, team_id, *, parts=None, report_ids=None):
        state = self.status(kind, team_id)
        if parts is not None:
            from .team_center import _validated_parts
            if kind != "team":
                raise ValueError("只有球队资料任务接受更新范围。")
            parts = list(_validated_parts(parts))
        if report_ids is not None:
            if (kind != "news" or not isinstance(report_ids, (list, tuple))
                    or any(not isinstance(identity, str) for identity in report_ids)):
                raise ValueError("只有新闻整理接受本队报告范围。")
            report_ids = tuple(dict.fromkeys(report_ids))
            for identity in report_ids:
                report = self.store.get("reports", identity)
                if not report or report.get("team_id") != team_id or report.get("mode") != "live":
                    raise ValueError("报告不存在，或不属于本队的真实资料。")
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
        entry = Work(kind, team_id, state["revision"] + 1, asyncio.Event(), parts=parts, report_ids=report_ids)
        state.update(revision=entry.revision, enabled=True, status="running", phase="starting",
                     message="正在接续已保存的工作。", started_at=now(), finished_at=None,
                     next_run_at=None, last_error=None, source_errors=[], updated_at=now(),
                     collection=None,
                     progress={"completed": 0, "failed": 0, "remaining": 0, **({"settled": 0} if kind == "team" else {})},
                     **({"leaves": [], "actionable_count": None, "panel": None} if kind == "team" else {}))
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
            collected = state.get("collection") if entry.kind == "news" else None
            incomplete = bool(state["last_error"] or progress["failed"] or progress["remaining"]
                              or collected and collected["status"] in {"failed", "partial"})
            useful = progress["completed"] or collected and (collected["readable_count"] or collected["available_sources"])
            outcome = ("partial" if useful else "failed") if incomplete else "completed"
            if collected and collected["status"] == "failed" and not progress["completed"]:
                outcome = "failed"
            note = (f"本次完成 {progress['completed']} 项，部分工作未完成，可手动重试。" if outcome == "partial"
                    else "本次未能完成，点击启动可重试。" if outcome == "failed"
                    else f"本次更新已完成，共完成 {progress['completed']} 项。")
            if entry.kind == "team" and (state.get("panel") or {}).get("summary"):
                note = state["panel"]["summary"]
            if collected and not progress["completed"] and not progress["failed"] and not progress["remaining"]:
                note = ({"no_matches": "已检查消息来源，暂未发现本队可整理的新消息。",
                         "outside_window": "已检查消息来源，暂未发现近 7 天可整理的新消息。",
                         "date_unknown": "已找到本队消息，但发布时间尚未核实，未作为近期动态整理。",
                         "date_pending": "已找到本队消息，本批仍有发布时间待核对，尚未作为近期动态整理。",
                         "out_of_scope": "找到的文章属于女足、青年队或其他范围，本轮未纳入男足一线队动态。",
                         "unreadable": "已找到消息链接，但本轮未能读取正文；查看来源说明后可重试。",
                         "sources_unavailable": "本轮未能读取消息来源，尚未进入中文整理。",
                         "readable": "已取得可读来源，暂无新增中文稿；已有动态保留。"}[collected["reason"]])
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
        authorized = set(entry.report_ids or ())
        def progress(value):
            rows = [row for row in news_reports.list_reports(self.store, team_id=entry.team_id) if row["id"] in authorized]
            queue = news_reports.queue_summary(self.store, team_id=entry.team_id, reports=rows)
            current = {"completed": base["completed"] + value["completed"], "failed": base["failed"] + value["failed"],
                       "remaining": queue["queued"] + queue["generating"]}
            self.update(entry, phase="organizing", message="正在阅读来源、整理并核对新闻。", progress=current)
            if value.get("stage") == "finish" and value.get("run_id"):
                run = self.store.get("runs", value["run_id"]) or {}
                if run.get("status") == "failed":
                    error = run.get("error") or ""
                    note = ("部分稿件尚未通过原文核验，已保留来源和其他完成稿。" if "核对" in error or "核验" in error
                            else "模型服务暂未完成部分任务，可检查模型设置后重新启动。" if any(word in error for word in ("模型", "协议", "服务"))
                            else "部分消息的来源暂时不可读，已保留其他完成内容。")
                    self.update(entry, last_error=note)
                if run.get("source_errors"):
                    state = self.status("news", entry.team_id)
                    self.update(entry, source_errors=research.unique_source_failures([*state.get("source_errors", []), *run["source_errors"]]))
        async def runner(store, run_id, cancel):
            run = store.get("runs", run_id)
            store.put("runs", {**run, "workflow_context": prompt_context(store, "news", entry.team_id)})
            async def execute(context):
                await run_agent(store, run_id, context.cancel_event, execution=context)
                return {"run_id": run_id}
            await self._unit(entry, execute, {"run_id": run_id})
        if entry.report_ids is None:
            self.update(entry, phase="discovering", message="正在查找并读取新的消息来源。")
            async def collect(context):
                return await asyncio.wait_for(research.collect_team(self.store, entry.team_id, query="", limit=10, max_age_days=7, force=first), 180)
            found = await self._unit(entry, collect, {"team_id": entry.team_id})
            collected = collection_summary(found)
            self.update(entry, collection=collected)
            prepared = news_reports.organize_reports(self.store, entry.team_id,
                event_ids=found.get("event_ids", []), retry_failed=False)
            authorized.update(prepared.get("report_ids", []))
            if prepared.get("unchanged_failed"):
                self.update(entry, last_error=f"{prepared['unchanged_failed']} 条消息此前未整理成功，来源和模型未变化，本轮未重复尝试。可检查模型配置或等待新的来源。")
            if found.get("errors"):
                state = self.status("news", entry.team_id)
                self.update(entry, source_errors=research.unique_source_failures([*state.get("source_errors", []), *found.get("source_errors", [])]))
        else:
            # A single-report retry authorizes its own sources only. The normal
            # agent pre-read validates those links before any drafting request.
            self.update(entry, phase="organizing", message="正在重读并整理所选消息来源。")
        base = self.status("news", entry.team_id)["progress"]
        await news_reports.process_report_queue(self.store, team_id=entry.team_id, owner_id=entry.root_id,
                                               cancel_event=entry.cycle_cancel, runner=runner, on_progress=progress,
                                               report_ids=authorized)
        # Which finished cards describe one story is a judgement, not a rule, so
        # it is asked once per refresh over the cards this team now has. A stale
        # or failed answer leaves the rule-based grouping in charge.
        try:
            from .personal_app import feed_view
            from .feed_grouping import regroup
            # ``raw`` is the card list before any rule-based collapse, which is
            # the set the grouping is keyed on.
            cards = feed_view(self.store, entry.team_id, None, 30, raw=True).get("items") or []
            await regroup(self.store, entry.team_id, cards)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _team_agent_configured(self):
        """Whether a usable model is assigned to the team-maintenance role.

        Two separate questions, and both have to be yes: something is assigned
        to the role, and that assignment actually resolves to an endpoint. A
        role pointing at a provider that has since been removed should fall back
        to the deterministic pass rather than fail the refresh.
        """
        from .model_config import role_selection, role_settings
        from .news_reports import _configured
        try:
            if not role_selection(self.store, "team"):
                return False
            return _configured(role_settings(self.store, "team"))
        except Exception:
            return False

    def _spawn_team_run(self, entry):
        """The run a model-driven refresh works through.

        It is an ordinary run in every respect except that no conversation owns
        it — the UI's start button does. Keeping it a run rather than a bespoke
        loop is what gives it the step log, the model budget, the delegation
        tree and the verification path that already exist for every other task.
        """
        from .model_config import role_selection
        import uuid as _uuid
        run_id = "maintain_" + _uuid.uuid4().hex
        selected = role_selection(self.store, "team") or {}
        run = {"id": run_id, "mode": "live", "team_id": entry.team_id,
               "team_name": (self.store.get("teams", entry.team_id) or {}).get("name") or entry.team_id,
               "skill": "team_refresh", "question": "按块核对并更新本球队的阵容、赛程、积分、照片、球队资料与球员中文名。",
               "conversation_id": run_id, "status": "queued", "answer": "", "evidence_ids": [],
               "warnings": [], "steps": [], "error": None, "created_at": now(), "finished_at": None,
               "internal_report": True, "model_role": "team",
               "provider_id": selected.get("provider_id"), "model": selected.get("model"),
               "allow_network": True, "spoiler": False, "input_evidence_ids": [],
               "workflow_context": prompt_context(self.store, "team", entry.team_id),
               "maintenance_parts": list(entry.parts) if entry.parts else None}
        self.store.put("runs", run)
        return run_id

    async def _team_agent_cycle(self, entry, first):
        """Run one model-driven refresh: the model reads the skill and delegates.

        Progress is reported from the run's own step log rather than from a
        second set of counters, so what the UI shows is what actually happened.
        """
        from .agent import run_agent
        run_id = self._spawn_team_run(entry)
        self.update(entry, phase="maintaining", message="正在读取维护技能，按块核对球队资料。")

        async def execute(context):
            await run_agent(self.store, run_id, context.cancel_event, execution=context)
            return {"run_id": run_id}

        outcome = await self._unit(entry, execute, {"run_id": run_id})
        run = self.store.get("runs", run_id) or {}
        outcome = {**outcome, "run": run}
        # A model-driven pass still leaves the deterministic bookkeeping to the
        # code: the components it did not touch, and the photo queue it may have
        # advanced, are read from the same view the old cycle used. The model
        # decides *what* to refresh; what "the club's data looks like now" means
        # is not a judgement call and does not become one.
        from .team_center import center_view
        self._record_team_progress(entry, run, center_view(self.store, entry.team_id))

    def _record_team_progress(self, entry, run, view):
        components = (view or {}).get("components", {})
        selected = entry.parts or ("roster", "schedule", "standings")
        good = sum(components.get(part, {}).get("status") in {"completed", "fresh", "partial", "stale"} for part in selected)
        bad = sum(components.get(part, {}).get("status") in {"failed", "unavailable", "interrupted"} for part in selected)
        note = None
        if run.get("status") == "failed":
            note = run.get("error") or "本轮维护未完成，已保留上次有效资料。"
        elif run.get("status") == "cancelled":
            note = "本轮维护已停止，已完成的部分已保存。"
        self.update(entry, progress={"completed": good, "failed": bad, "remaining": max(0, len(selected) - good - bad)},
                    last_error=note or ("部分球队资料暂未更新，仍可查看上次有效资料。" if bad else None))

    async def _team_cycle(self, entry, first):
        # An explicit parts list is the legacy maintenance call. The panel
        # start sends no parts and runs the six-leaf snapshot instead.
        if entry.parts is None:
            from .team_panel import run_panel
            await run_panel(self, entry, first)
            return
        await self._team_cycle_targeted(entry, first)

    async def _team_cycle_targeted(self, entry, first):
        from .team_center import maintain_team, center_view, sync_portraits
        if self._team_agent_configured():
            await self._team_agent_cycle(entry, first)
            return
        # No model is assigned to the team role, so there is nothing to delegate
        # and the deterministic pass is what runs. This is the path the app used
        # before the maintenance skill existed, kept intact on purpose: a user
        # who has configured no model still gets their club's data refreshed,
        # and gets it without a provider round-trip they never opted into.
        self.update(entry, phase="maintaining", message="正在核对并更新阵容、赛程和积分。")
        async def maintain(context):
            async with self.team_slots:
                return await maintain_team(self.store, entry.team_id, force=first, **({"parts": entry.parts} if entry.parts else {}))
        outcome = await self._unit(entry, maintain, {"team_id": entry.team_id})
        components = outcome.get("components", {})
        selected = entry.parts or ("roster", "schedule", "standings")
        good = sum(components.get(p, {}).get("status") in {"completed", "fresh", "partial"} for p in selected)
        bad = sum(components.get(p, {}).get("status") in {"failed", "unavailable", "interrupted"} for p in selected)
        self.update(entry, progress={"completed": good, "failed": bad, "remaining": max(0, len(selected)-good-bad)},
                    last_error="部分球队资料暂未更新，仍可查看上次有效资料。" if outcome.get("errors") else None)
        view = center_view(self.store, entry.team_id)
        # This is the active UI start route. The legacy maintenance manager is
        # not used here; all supported squads can use the Wikimedia provider.
        # A roster refresh rebuilds rows from the provider and drops their
        # photos, so "portraits are fresh" must not skip the phase when the
        # current roster is actually missing images.
        portraits_state = view.get("components", {}).get("portraits", {})
        roster_rows = view.get("roster") or []
        photos_present = sum(bool(row.get("photo_url")) for row in roster_rows)
        if entry.parts is None and view.get("roster") and (
                portraits_state.get("status") != "fresh" or photos_present < len(roster_rows)):
            self.update(entry, phase="portraits", message="核心资料已保存，正在补充可核对的球员照片。")
            # The outer bound stays at 180s, but the pass itself is told it has
            # less: a batch sized against the full window can still be running
            # when the wait expires, and that is reported as interrupted rather
            # than as the partial progress it actually is.
            photo_budget = 150
            async def portraits(context):
                # Bounded so one slow publisher cannot hold the whole cycle, but
                # wide enough for a full batch at the paced Wikimedia rate;
                # otherwise the batch is truncated mid-way.
                return await asyncio.wait_for(
                    sync_portraits(self.store, entry.team_id, force=first, budget_seconds=photo_budget,
                                   review=True), 180)
            photos = await self._unit(entry, portraits, {"team_id": entry.team_id, "part": "portraits"})
            photo_complete = photos.get("status") == "completed"
            photo_failed = photos.get("status") in {"failed", "unavailable", "interrupted"}
            state = self.status(entry.kind, entry.team_id)
            progress = state["progress"]
            note = None
            if not photo_complete:
                if photos.get("errors"):
                    note = "球员照片来源暂时无法读取，已保留核心资料和通过核验的照片。"
                elif photos.get("remaining_count"):
                    note = f"已核对本批照片，还有 {photos['remaining_count']} 名球员待核对；再次启动会接续补图。"
                else:
                    note = "部分球员暂无通过身份与许可核验的照片，已保留缺图原因。"
            self.update(entry, progress={"completed": progress["completed"] + int(photo_complete),
                                         "failed": progress["failed"] + int(photo_failed),
                                         "remaining": progress["remaining"] + int(not photo_complete and not photo_failed)},
                        last_error="；".join(value for value in (state.get("last_error"), note) if value) or None)
