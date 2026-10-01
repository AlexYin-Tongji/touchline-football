"""Cold news discovery and retry boundaries use isolated synthetic state."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from server import news_reports
from server.workflows import WorkflowManager
from tests.test_workflows import MemoryStore
from tests.test_news_reports import event, successful_runner


class DiscoveryOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_out_of_scope_articles_are_not_labelled_as_unreadable_sources(self):
        state = await self.run_collection({"sources": [{"state": "fresh", "relevant_count": 4, "window_count": 4}],
            "errors": [], "attempted_reads": 4, "scope_rejected_count": 4, "evidence_ids": [], "event_ids": []})
        self.assertEqual(state["collection"]["status"], "empty")
        self.assertEqual(state["collection"]["reason"], "out_of_scope")
        self.assertEqual(state["collection"]["scope_rejected_count"], 4)
        self.assertNotEqual(state["status"], "failed")

    async def run_collection(self, found):
        store = MemoryStore()
        manager = WorkflowManager(store)
        with patch("server.research.collect_team", new=AsyncMock(return_value=found)), \
             patch("server.agent.run_agent", new=AsyncMock()) as model:
            try:
                manager.start("news", "arsenal")
                await asyncio.wait_for(manager.tasks[("news", "arsenal")].task, 1)
                state = manager.status("news", "arsenal")
                model.assert_not_called()
                self.assertEqual(store.list("runs"), [])
                self.assertEqual(state["progress"], {"completed": 0, "failed": 0, "remaining": 0})
                return state
            finally:
                await manager.close()

    async def test_available_empty_index_is_checked_without_inventing_editorial_failure(self):
        state = await self.run_collection({"sources": [{"state": "fresh", "matched_count": 0}, {"state": "unavailable"}],
            "errors": ["另一来源 HTTP 403"], "indexed_count": 70, "relevant_count": 0, "evidence_ids": []})
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["collection"]["reason"], "no_matches")
        self.assertEqual(state["collection"]["indexed_count"], 70)
        self.assertIn("暂未发现", state["message"])

    async def test_unreadable_sources_and_bodies_are_distinct_failures_before_generation(self):
        for found, reason in (({"sources": [{"state": "unavailable"}], "errors": ["来源超时"]}, "sources_unavailable"),
                              ({"sources": [{"state": "fresh"}], "read_attempted": 3, "readable_count": 0,
                                "relevant_count": 3, "dated_count": 3, "window_count": 3, "errors": ["正文超时"]}, "unreadable")):
            with self.subTest(reason=reason):
                state = await self.run_collection(found)
                self.assertEqual(state["status"], "failed")
                self.assertEqual(state["collection"]["reason"], reason)
                self.assertIn("来源" if reason == "sources_unavailable" else "正文", state["message"])

    async def test_unknown_dates_and_old_news_never_claim_fresh_material(self):
        base = {"sources": [{"state": "fresh"}], "relevant_count": 4, "window_count": 0, "errors": []}
        unknown = await self.run_collection({**base, "dated_count": 0})
        self.assertEqual(unknown["status"], "partial")
        self.assertIn("发布时间尚未核实", unknown["message"])
        old = await self.run_collection({**base, "dated_count": 4})
        self.assertEqual(old["status"], "completed")
        self.assertEqual(old["collection"]["reason"], "outside_window")
        pending = await self.run_collection({"sources": [{"state": "fresh", "indexed_count": 10,
            "relevant_count": 4, "dated_count": 2, "window_count": 0, "date_pending_count": 2}], "errors": []})
        self.assertEqual(pending["status"], "partial")
        self.assertEqual(pending["collection"]["reason"], "date_pending")
        self.assertEqual(pending["collection"]["indexed_count"], 10)

    async def test_failed_draft_is_not_also_counted_as_pending(self):
        store = MemoryStore(); event(store)
        manager = WorkflowManager(store)
        async def fail(store, run_id, cancel, **kwargs):
            store.put("runs", {**store.get("runs", run_id), "status": "failed", "error": "报告未通过完整原文核对"})
        with patch("server.research.collect_team", new=AsyncMock(return_value={"sources": [{"state": "fresh"}], "errors": [],
            "event_ids": ["event_one"], "evidence_ids": ["ev_injury"]})), \
             patch("server.agent.run_agent", new=fail):
            try:
                manager.start("news", "arsenal")
                await asyncio.wait_for(manager.tasks[("news", "arsenal")].task, 1)
                self.assertEqual(manager.status("news", "arsenal")["progress"],
                                 {"completed": 0, "failed": 1, "remaining": 0})
            finally:
                await manager.close()

    async def test_core_failures_are_terminal_units_not_remaining_units(self):
        store = MemoryStore()
        manager = WorkflowManager(store)
        result = {"components": {part: {"status": "failed"} for part in ("roster", "schedule", "standings")},
                  "errors": ["来源超时"]}
        with patch("server.team_center.maintain_team", new=AsyncMock(return_value=result)), \
             patch("server.team_center.center_view", return_value={"roster": [], "components": {}}):
            try:
                manager.start("team", "arsenal")
                await asyncio.wait_for(manager.tasks[("team", "arsenal")].task, 1)
                self.assertEqual(manager.status("team", "arsenal")["progress"],
                                 {"completed": 0, "failed": 3, "remaining": 0})
            finally:
                await manager.close()


class EditorialRetryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def fail_once(self, store, error):
        row = news_reports.enqueue_reports(store)[0]
        async def fail(store, run_id, cancel):
            store.put("runs", {**store.get("runs", run_id), "status": "failed", "error": error})
        await news_reports.process_report_queue(store, runner=fail)
        return store.get("reports", row["id"]), fail

    async def test_review_and_unknown_failures_do_not_loop_after_backoff(self):
        for error in ("报告未通过完整原文核对，已保留来源链接。", "后台整理未完成", "来源返回 HTTP 404"):
            with self.subTest(error=error):
                store = MemoryStore(); event(store)
                row, _ = await self.fail_once(store, error)
                store.put("reports", {**row, "updated_at": "2000-01-01T00:00:00Z"})
                runner = AsyncMock(side_effect=successful_runner)
                self.assertEqual((await news_reports.process_report_queue(store, runner=runner))["processed"], 0)
                runner.assert_not_called()
                self.assertFalse(news_reports.get_report(store, row["id"])["automatic_retry_eligible"])

    async def test_transient_automatic_attempts_have_backoff_and_a_total_cap(self):
        store = MemoryStore(); event(store)
        row, fail = await self.fail_once(store, "模型请求超时，请稍后重试。")
        self.assertTrue(news_reports.get_report(store, row["id"])["automatic_retry_eligible"])
        self.assertEqual((await news_reports.process_report_queue(store, runner=fail))["processed"], 0)
        for _ in range(news_reports.FAILED_REPORT_ATTEMPT_LIMIT + 1):
            current = store.get("reports", row["id"])
            store.put("reports", {**current, "updated_at": "2000-01-01T00:00:00Z"})
            await news_reports.process_report_queue(store, runner=fail)
        self.assertEqual(len(store.list("runs")), news_reports.FAILED_REPORT_ATTEMPT_LIMIT)
        self.assertFalse(news_reports.get_report(store, row["id"])["automatic_retry_eligible"])

    async def test_connection_interruption_can_recover_without_reauthorizing_cancelled_work(self):
        store = MemoryStore(); event(store)
        row, _ = await self.fail_once(store, "模型连接中断，请重试。")
        store.put("reports", {**row, "updated_at": "2000-01-01T00:00:00Z"})
        self.assertEqual((await news_reports.process_report_queue(store, runner=successful_runner))["completed"], 1)

    async def test_manual_transient_failure_requires_a_new_explicit_batch(self):
        store = MemoryStore(); event(store)
        news_reports.organize_reports(store, "arsenal")
        async def fail(store, run_id, cancel):
            store.put("runs", {**store.get("runs", run_id), "status": "failed", "error": "模型请求超时"})
        await news_reports.process_report_queue(store, runner=fail)
        row = store.list("reports")[0]
        store.put("reports", {**row, "updated_at": "2000-01-01T00:00:00Z"})
        runner = AsyncMock(side_effect=successful_runner)
        self.assertEqual((await news_reports.process_report_queue(store, runner=runner))["processed"], 0)
        runner.assert_not_called()
        self.assertFalse(news_reports.get_report(store, row["id"])["automatic_retry_eligible"])
        self.assertEqual(news_reports.organize_reports(store, "arsenal")["queued"], 1)
        self.assertEqual((await news_reports.process_report_queue(store, runner=runner))["completed"], 1)
