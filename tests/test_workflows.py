"""User-owned single-pass work; synthetic executors and isolated state only."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.test_agent import FakeStore


class MemoryStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.meta = {}

    def get_meta(self, key, default=None):
        import copy
        return copy.deepcopy(self.meta.get(key, default))

    def set_meta(self, key, value):
        import copy
        self.meta[key] = copy.deepcopy(value)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_start_finishes_one_cycle_without_scheduling_another(self):
        from server.workflows import WorkflowManager
        store = MemoryStore()
        for expected, progress, error in (
            ("completed", {"completed": 2, "failed": 0, "remaining": 0}, False),
            ("partial", {"completed": 1, "failed": 1, "remaining": 1}, False),
            ("partial", {"completed": 1, "failed": 0, "remaining": 0}, True),
            ("failed", {"completed": 0, "failed": 0, "remaining": 0}, True),
        ):
            with self.subTest(expected=expected, error=error):
                calls = []
                async def cycle(manager, entry, first):
                    calls.append(entry.root_id)
                    manager.update(entry, progress=progress)
                    if error:
                        raise RuntimeError("synthetic unavailable source")
                manager = WorkflowManager(store, cycle_runner=cycle)
                try:
                    for attempt in range(2):
                        self.assertTrue(manager.start("news", "arsenal")["started"])
                        task = manager.tasks[("news", "arsenal")].task
                        try:
                            await asyncio.wait_for(asyncio.shield(task), .5)
                        except TimeoutError:
                            self.fail("One manual update must finish without waiting for another cycle")
                        result = manager.status("news", "arsenal")
                        self.assertEqual(result["status"], expected)
                        self.assertFalse(result["enabled"])
                        self.assertIsNone(result["next_run_at"])
                        self.assertIsNotNone(result["finished_at"])
                        self.assertEqual(result["progress"], progress)
                        self.assertFalse(manager.tasks)
                        self.assertEqual(len(calls), attempt + 1)
                    await manager.close()
                    self.assertEqual(manager.status("news", "arsenal")["status"], expected)
                finally:
                    await manager.close()

    async def test_start_is_idempotent_stop_awaits_cleanup_and_keeps_memory(self):
        from server.workflows import WorkflowManager
        store = MemoryStore()
        started, cleaned = asyncio.Event(), asyncio.Event()
        calls = 0
        async def cycle(manager, entry, first):
            nonlocal calls
            calls += 1
            manager.update(entry, progress={"completed": 2, "failed": 0, "remaining": 1})
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        manager = WorkflowManager(store, cycle_runner=cycle)
        try:
            self.assertTrue(manager.start("news", "arsenal")["started"])
            self.assertFalse(manager.start("news", "arsenal")["started"])
            await started.wait()
            result = await manager.stop("news", "arsenal")
            self.assertFalse(result["enabled"])
            self.assertEqual(result["status"], "stopped")
            self.assertEqual(result["progress"]["completed"], 2)
            self.assertEqual(result["memory"]["completed_count"], 2)
            self.assertTrue(cleaned.is_set())
            self.assertEqual(calls, 1)
        finally:
            await manager.close()

    async def test_completed_news_is_not_regenerated_after_stop_and_restart(self):
        from server.workflows import WorkflowManager
        from tests.test_news_reports import event, successful_runner
        store = MemoryStore()
        event(store)
        manager = WorkflowManager(store)
        model_runs = []
        async def runner(store, rid, cancel, **kwargs):
            model_runs.append(rid)
            context = json.loads(store.get("runs", rid)["workflow_context"])
            self.assertEqual(context["功能"], "球队动态")
            self.assertEqual(context["球队"], "arsenal")
            await successful_runner(store, rid, cancel)
        async def completed():
            async with asyncio.timeout(1):
                while manager.status("news", "arsenal")["status"] != "completed":
                    await asyncio.sleep(.001)
        with patch("server.agent.run_agent", new=runner), patch("server.research.collect_team", new=AsyncMock(return_value={"errors": [], "evidence_ids": []})):
            try:
                manager.start("news", "arsenal")
                await completed()
                self.assertEqual(manager.status("news", "arsenal")["progress"]["completed"], 1)
                await manager.stop("news", "arsenal")
                manager.start("news", "arsenal")
                await completed()
                self.assertEqual(len(model_runs), 1)
                self.assertEqual(store.list("reports")[0]["status"], "completed")
            finally:
                await manager.close()

    async def test_stop_request_cancellation_cannot_abandon_cleanup(self):
        from server.workflows import WorkflowManager
        store = MemoryStore()
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def cycle(manager, entry, first):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        manager = WorkflowManager(store, cycle_runner=cycle)
        manager.start("news", "arsenal")
        await entered.wait()
        waiter = asyncio.create_task(manager.stop("news", "arsenal"))
        try:
            await cleaning.wait()
            self.assertFalse(manager.start("news", "arsenal")["started"])
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            release.set()
            await manager.close()
            self.assertFalse(manager.tasks)
            self.assertFalse(manager.status("news", "arsenal")["enabled"])
        finally:
            release.set()
            await manager.close()

    def test_progress_memory_cannot_promote_arbitrary_summary_to_evidence(self):
        from server.task_memory import prompt_context, state_key
        store = MemoryStore()
        store.set_meta(state_key("news", "arsenal"), {"memory": {"summary": "PRIVATE_TRANSCRIPT", "completed_count": 3}, "progress": {"completed": 1, "remaining": 2}})
        context = prompt_context(store, "news", "arsenal")
        self.assertNotIn("PRIVATE_TRANSCRIPT", context)
        self.assertEqual(json.loads(context)["历史完成数"], 3)


class WorkflowAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_and_page_reads_do_not_revive_legacy_schedules(self):
        import httpx
        from server.main import create_app
        from server.task_memory import state_key
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory) / "manual.db")
            store = app.state.store
            store.save_settings({"auto_refresh": True, "automation": {"enabled": True},
                                 "reports": {"auto_generate": True}})
            store.set_meta(state_key("news", "arsenal"), {"enabled": True, "status": "waiting",
                                                         "next_run_at": "2000-01-01T00:00:00+00:00"})
            store.put("tracking", {"id": "old_schedule", "mode": "live", "team_id": "arsenal",
                "query": "training", "title": "training", "enabled": True, "status": "idle",
                "interval_minutes": 15, "next_check": "2000-01-01T00:00:00+00:00"})
            original_sleep = asyncio.sleep
            async def advance_scheduler(delay, *args, **kwargs):
                await original_sleep(.001 if delay == 30 else delay, *args, **kwargs)
            with patch("server.main.asyncio.sleep", new=advance_scheduler), \
                 patch("server.research.collect_team", new=AsyncMock(return_value={"errors": [], "evidence_ids": []})), \
                 patch("server.agent.run_agent", new=AsyncMock()):
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                        responses = []
                        for path in ("/api/feed?team_id=arsenal", "/api/teams/arsenal/center", "/api/workflows/news/arsenal"):
                            responses.append((await client.get(path)).status_code)
                        await original_sleep(.03)
                        runs = store.list("runs", "live")
                        active = bool(app.state.workflows.tasks or app.state.personal.tasks or app.state.team_maintenance.tasks)
                        enabled = app.state.workflows.status("news", "arsenal")["enabled"]
                        next_check = store.get("tracking", "old_schedule")["next_check"]
            self.assertEqual(responses, [200, 200, 200])
            self.assertEqual(runs, [])
            self.assertFalse(active)
            self.assertFalse(enabled)
            self.assertIsNone(next_check)

    async def test_legacy_targeted_maintenance_preserves_requested_parts(self):
        import httpx
        from server.main import create_app
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory) / "parts.db")
            observed, done = [], asyncio.Event()
            async def maintain(store, team_id, **kwargs):
                observed.append(kwargs.get("parts"))
                done.set()
                return {"components": {"portraits": {"status": "completed"}}}
            try:
                with patch("server.team_center.maintain_team", new=maintain):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                        self.assertEqual((await client.post("/api/teams/arsenal/maintain", json={"parts": ["portraits"]})).status_code, 202)
                        await asyncio.wait_for(done.wait(), 1)
                        self.assertEqual(observed, [["portraits"]])
            finally:
                await app.state.workflows.close()
                app.state.store.close()

    async def test_explicit_report_retry_starts_its_owned_news_workflow(self):
        import httpx
        from server.main import create_app
        from server import news_reports
        from tests.test_news_reports import event
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory) / "retry.db")
            store = app.state.store
            store.save_settings(MemoryStore().settings())
            store.put("evidence", MemoryStore().get("evidence", "ev_injury"))
            event(store)
            report = news_reports.enqueue_reports(store, team_id="arsenal")[0]
            store.put("reports", {**report, "status": "failed", "attempts": 3})
            entered = asyncio.Event()
            async def cycle(manager, entry, first):
                entered.set()
                await asyncio.Event().wait()
            app.state.workflows.cycle_runner = cycle
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                    response = await client.post(f"/api/reports/{report['id']}/retry")
                    self.assertEqual(response.status_code, 200)
                    await asyncio.wait_for(entered.wait(), 1)
                    state = (await client.get("/api/workflows/news/arsenal")).json()
                    self.assertTrue(state["enabled"])
            finally:
                await app.state.workflows.close()
                store.close()

    async def test_passive_reads_configuration_and_start_stop_contract(self):
        import httpx
        from server.main import create_app
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory) / "workflow.db")
            store = app.state.store
            calls = []
            started = asyncio.Event()
            async def cycle(manager, entry, first):
                calls.append((entry.kind, entry.team_id))
                started.set()
                await asyncio.Event().wait()
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                    for path in ("/api/feed?team_id=arsenal", "/api/teams/arsenal/center", "/api/workflows/news/arsenal"):
                        self.assertEqual((await client.get(path)).status_code, 200)
                    self.assertFalse(app.state.workflows.tasks)
                    self.assertFalse(app.state.team_maintenance.tasks)
                    blocked = (await client.post("/api/workflows/news/arsenal/start")).json()
                    self.assertEqual(blocked["status"], "blocked")
                    app.state.workflows.cycle_runner = cycle
                    first = (await client.post("/api/workflows/news/arsenal/start")).json()
                    second = (await client.post("/api/workflows/news/arsenal/start")).json()
                    self.assertTrue(first["started"])
                    self.assertFalse(second["started"])
                    await started.wait()
                    self.assertEqual((await client.post("/api/workflows/news/arsenal/stop")).json()["status"], "stopped")
                    await client.get("/api/teams/arsenal/center")
                    await client.get("/api/feed?team_id=arsenal")
                    self.assertEqual(calls, [("news", "arsenal")])
                    self.assertFalse(app.state.workflows.tasks)
            finally:
                await app.state.workflows.close()
                store.close()

    async def test_idle_reads_do_not_start_and_completed_work_stays_idle(self):
        from server.workflows import WorkflowManager
        store = MemoryStore()
        cycle_done = asyncio.Event()
        calls = 0
        async def cycle(manager, entry, first):
            nonlocal calls
            calls += 1
            cycle_done.set()
        manager = WorkflowManager(store, cycle_runner=cycle)
        try:
            self.assertEqual(manager.status("news", "arsenal")["status"], "idle")
            self.assertEqual(calls, 0)
            manager.start("news", "arsenal")
            await cycle_done.wait()
            async with asyncio.timeout(1):
                while manager.status("news", "arsenal")["status"] != "completed":
                    await asyncio.sleep(.001)
            self.assertEqual(manager.status("news", "arsenal")["status"], "completed")
            for _ in range(5):
                manager.status("news", "arsenal")
            self.assertEqual(calls, 1)
        finally:
            await manager.close()

    async def test_stop_one_team_does_not_cancel_another_and_restart_is_explicit(self):
        from server.workflows import WorkflowManager
        store = MemoryStore()
        store.put("teams", {"id": "liverpool", "name": "利物浦"})
        active = set()
        ready = asyncio.Event()
        async def cycle(manager, entry, first):
            active.add(entry.team_id)
            if len(active) == 2:
                ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                active.discard(entry.team_id)
        manager = WorkflowManager(store, cycle_runner=cycle)
        try:
            manager.start("news", "arsenal")
            manager.start("news", "liverpool")
            await ready.wait()
            await manager.stop("news", "arsenal")
            self.assertEqual(active, {"liverpool"})
            await manager.close()
            restored = WorkflowManager(store, cycle_runner=cycle)
            restored.recover()
            self.assertFalse(restored.status("news", "liverpool")["enabled"])
            self.assertFalse(restored.tasks)
            await restored.close()
        finally:
            await manager.close()
