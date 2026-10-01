"""Refreshing sources never authorizes an unrelated historical editorial batch."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from server import news_reports
from server.workflows import WorkflowManager
from tests.test_workflows import MemoryStore
from tests.test_news_reports import event, successful_runner


def archived_report(store, *, status="failed"):
    source = store.get("evidence", "ev_injury")
    store.put("evidence", {**source, "id": "archive_evidence", "title": "Historical training update"})
    store.put("events", {"id": "archive_event", "team_id": "arsenal", "mode": "live",
        "title": "Archived event", "evidence_ids": ["archive_evidence"]})
    row = news_reports.enqueue_reports(store, event_ids=["archive_event"])[0]
    row.update(status=status, attempts=2, error="报告未通过完整原文核对。", manual_stop_after_attempt=True,
               attempt_history=[{"at": "2000-01-01T00:00:00Z", "kind": "manual"}])
    return store.put("reports", row)


class SourceFirstWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def run_cycle(self, store, found, runner, *, report_ids=None):
        manager = WorkflowManager(store)
        try:
            with patch("server.research.collect_team", new=AsyncMock(return_value=found)), \
                 patch("server.agent.run_agent", new=runner):
                manager.start("news", "arsenal", report_ids=report_ids)
                await asyncio.wait_for(manager.tasks[("news", "arsenal")].task, 1)
                return manager.status("news", "arsenal")
        finally:
            await manager.close()

    async def test_source_discovery_precedes_generation_and_only_fresh_events_are_authorized(self):
        store = MemoryStore()
        old = archived_report(store)
        event(store, "fresh")
        trace = []
        async def collect(*args, **kwargs):
            trace.append("collect")
            self.assertEqual(store.get("reports", old["id"]), old)
            return {"errors": [], "event_ids": ["event_fresh"], "evidence_ids": ["ev_injury"]}
        async def runner(store, run_id, cancel, **kwargs):
            trace.append("generate")
            self.assertEqual(store.get("runs", run_id)["event_id"], "event_fresh")
            await successful_runner(store, run_id, cancel)
        manager = WorkflowManager(store)
        with patch("server.research.collect_team", new=collect), patch("server.agent.run_agent", new=runner):
            try:
                manager.start("news", "arsenal")
                await asyncio.wait_for(manager.tasks[("news", "arsenal")].task, 1)
                self.assertEqual(trace, ["collect", "generate"])
                self.assertEqual(manager.status("news", "arsenal")["progress"],
                                 {"completed": 1, "failed": 0, "remaining": 0})
                self.assertEqual(store.get("reports", old["id"]), old)
            finally:
                await manager.close()

    async def test_empty_or_failed_collection_does_not_drain_old_queued_or_failed_reports(self):
        for found in ({"sources": [{"state": "fresh"}], "errors": [], "event_ids": []},
                      {"sources": [{"state": "unavailable"}], "errors": ["source timeout"], "event_ids": []}):
            with self.subTest(collection=found):
                store = MemoryStore()
                old = archived_report(store, status="queued")
                # Make the old queued row executable to prove the filter, rather
                # than relying on the manual single-attempt guard to skip it.
                old.pop("manual_stop_after_attempt")
                store.put("reports", old)
                model = AsyncMock()
                await self.run_cycle(store, found, model)
                model.assert_not_called()
                self.assertEqual(store.list("runs"), [])
                self.assertEqual(store.get("reports", old["id"]), old)

    async def test_unchanged_failed_draft_needs_explicit_retry_even_when_its_source_is_found_again(self):
        store = MemoryStore(); event(store)
        row = news_reports.enqueue_reports(store)[0]
        row.update(status="failed", attempts=1, error="报告未通过完整原文核对。", manual_stop_after_attempt=True)
        store.put("reports", row)
        model = AsyncMock()
        state = await self.run_cycle(store, {"errors": [], "event_ids": ["event_one"], "evidence_ids": ["ev_injury"]}, model)
        model.assert_not_called()
        self.assertEqual(store.get("reports", row["id"]), row)
        self.assertEqual(state["status"], "partial")
        self.assertIn("本轮未重复尝试", state["last_error"])

    async def test_explicit_retry_reauthorizes_only_that_manual_failure_and_preserves_history(self):
        store = MemoryStore(); event(store)
        clicked = news_reports.enqueue_reports(store)[0]
        clicked.update(status="failed", attempts=1, manual_stop_after_attempt=True,
                       attempt_history=[{"at": "2000-01-01T00:00:00Z", "kind": "manual"}])
        store.put("reports", clicked)
        old = archived_report(store)
        self.assertEqual(news_reports.retry_report(store, clicked["id"])["status"], "failed")
        self.assertEqual(news_reports.retry_report(store, clicked["id"], explicit=True)["status"], "queued")
        seen = []
        async def runner(store, run_id, cancel, **kwargs):
            seen.append(store.get("runs", run_id)["report_id"])
            await successful_runner(store, run_id, cancel)
        state = await self.run_cycle(store, {"sources": [{"state": "fresh"}], "errors": [], "event_ids": []},
                                     runner, report_ids=[clicked["id"]])
        self.assertEqual(seen, [clicked["id"]])
        self.assertEqual(state["progress"]["remaining"], 0)
        self.assertEqual(store.get("reports", old["id"]), old)
        saved = store.get("reports", clicked["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["attempt_history"][:1], clicked["attempt_history"])
        self.assertEqual(len(saved["attempt_history"]), 2)
        self.assertTrue(saved["manual_stop_after_attempt"])

    async def test_explicit_retry_does_not_authorize_an_unrelated_new_event(self):
        store = MemoryStore(); event(store, "fresh")
        # The selected historical job has the same visible synthetic source,
        # but a distinct event/report. No other pending event is authorized.
        event(store, "clicked")
        clicked = news_reports.enqueue_reports(store, event_ids=["event_clicked"])[0]
        store.put("reports", {**clicked, "status": "failed", "manual_stop_after_attempt": True})
        news_reports.retry_report(store, clicked["id"], explicit=True)
        old = archived_report(store)
        seen = []
        async def runner(store, run_id, cancel, **kwargs):
            seen.append(store.get("runs", run_id)["event_id"])
            await successful_runner(store, run_id, cancel)
        state = await self.run_cycle(store, {"errors": [], "event_ids": ["event_fresh"], "evidence_ids": ["ev_injury"]},
                                     runner, report_ids=[clicked["id"]])
        self.assertEqual(seen, ["event_clicked"])
        self.assertEqual(state["progress"], {"completed": 1, "failed": 0, "remaining": 0})
        self.assertFalse(any(row.get("event_id") == "event_fresh" for row in store.list("reports")))
        self.assertEqual(store.get("reports", old["id"]), old)

    async def test_unrelated_generating_owner_is_neither_stolen_nor_allowed_to_block_new_scope(self):
        store = MemoryStore()
        old = archived_report(store, status="generating")
        old.update(manual_report=True, run_id="another_owner")
        store.put("reports", old)
        store.put("runs", {"id": "another_owner", "status": "running", "team_id": "arsenal", "mode": "live"})
        event(store, "fresh")
        model = AsyncMock(side_effect=successful_runner)
        await self.run_cycle(store, {"errors": [], "event_ids": ["event_fresh"], "evidence_ids": ["ev_injury"]}, model)
        self.assertEqual(model.await_count, 1)
        self.assertEqual(store.get("reports", old["id"]), old)
        self.assertEqual(store.get("runs", "another_owner")["status"], "running")

    async def test_stop_during_discovery_never_spends_on_historical_material(self):
        store = MemoryStore()
        old = archived_report(store)
        entered = asyncio.Event()
        async def collect(*args, **kwargs):
            entered.set()
            await asyncio.Future()
        manager = WorkflowManager(store)
        with patch("server.research.collect_team", new=collect), patch("server.agent.run_agent", new=AsyncMock()) as model:
            try:
                manager.start("news", "arsenal")
                await asyncio.wait_for(entered.wait(), 1)
                await manager.stop("news", "arsenal")
                model.assert_not_called()
                self.assertEqual(store.get("reports", old["id"]), old)
                self.assertEqual(manager.status("news", "arsenal")["status"], "stopped")
            finally:
                await manager.close()

    async def test_explicit_report_scope_rejects_other_teams_and_demo_without_starting(self):
        store = MemoryStore()
        row = archived_report(store)
        manager = WorkflowManager(store)
        try:
            for change in ({"team_id": "liverpool"}, {"mode": "demo"}):
                store.put("reports", {**row, **change})
                with self.assertRaises(ValueError):
                    manager.start("news", "arsenal", report_ids=[row["id"]])
                self.assertFalse(manager.tasks)
                self.assertEqual(manager.status("news", "arsenal")["status"], "idle")
        finally:
            await manager.close()

    async def test_queue_scope_preserves_unrelated_eligible_jobs(self):
        store = MemoryStore()
        old = archived_report(store, status="queued")
        old.pop("manual_stop_after_attempt")
        store.put("reports", old)
        event(store, "fresh")
        fresh = news_reports.enqueue_reports(store, event_ids=["event_fresh"])[0]
        model = AsyncMock(side_effect=successful_runner)
        result = await news_reports.process_report_queue(store, runner=model, report_ids=[fresh["id"]])
        self.assertEqual(result["completed"], 1)
        self.assertEqual(model.await_count, 1)
        self.assertEqual(store.get("reports", old["id"]), old)
