"""Explicit editorial batches use isolated stores and never real providers."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from server import news_reports, personal_app
from server.main import create_app
from tests.test_agent import FakeStore
from tests.test_feed_recovery import MemoryStore
from tests.test_news_reports import event, successful_runner


async def failure_runner(store, run_id, cancel):
    store.put("runs", {**store.get("runs", run_id), "status": "failed", "error": "测试模型暂不可用"})


class ManualReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MemoryStore()
        event(self.store)

    def existing_attempt_history(self):
        row = news_reports.enqueue_reports(self.store)[0]
        row["attempt_history"] = [news_reports.now().isoformat()] * 12
        self.store.put("reports", row)
        return row

    async def test_manual_request_finishes_all_pending_and_preserves_attempt_history(self):
        for number in range(15):
            event(self.store, str(number))
        original = self.existing_attempt_history()
        request = news_reports.organize_reports(self.store, "arsenal")
        self.assertEqual((request["started"], request["queued"]), (True, 16))
        public = news_reports.get_report(self.store, original["id"])
        self.assertTrue(public["manual_organize"])
        self.assertNotIn("manual_once", public)
        self.assertNotIn("manual_stop_after_attempt", public)
        self.assertNotIn("attempt_history", public)
        summary = personal_app.feed_view(self.store, "arsenal")["processing"]
        self.assertEqual((summary["manual_count"], summary["queued"]), (16, 16))
        self.assertFalse(summary["can_organize"])
        self.assertEqual((await news_reports.process_report_queue(self.store, runner=successful_runner))["completed"], 16)
        self.assertEqual((await news_reports.process_report_queue(self.store, runner=successful_runner))["processed"], 0)
        budget = news_reports.report_budget(self.store)
        self.assertEqual((budget["used_today"], budget["remaining_today"], budget["manual_used_today"]), (12, None, 16))
        self.assertIsNone(budget["daily_limit"])
        self.assertIsNone(budget["manual_batch_limit"])
        self.assertIsNone(budget["resets_at"])
        saved = self.store.get("reports", original["id"])
        self.assertEqual(saved["attempt_history"][:12], original["attempt_history"])
        self.assertEqual(saved["attempt_history"][-1]["kind"], "manual")
        self.assertFalse(saved["manual_once"])
        self.assertTrue(all(run["internal_report"] and run["manual_report"] and run["model_role"] == "feed"
                            for run in self.store.list("runs")))
        self.assertEqual(personal_app.feed_view(self.store, "arsenal")["processing"]["manual_count"], 0)
        self.assertEqual(news_reports.organize_reports(self.store, "arsenal")["queued"], 0)

    async def test_duplicate_clicks_while_queued_and_running_do_not_extend_batch(self):
        for number in range(4):
            event(self.store, str(number))
        news_reports.organize_reports(self.store, "arsenal")
        self.assertEqual(news_reports.organize_reports(self.store, "arsenal")["status"], "busy")
        seen = []
        async def observing_runner(store, run_id, cancel):
            seen.append(news_reports.organize_reports(store, "arsenal"))
            await successful_runner(store, run_id, cancel)
        result = await news_reports.process_report_queue(self.store, runner=observing_runner)
        self.assertEqual(result["completed"], 5)
        self.assertTrue(all(item["status"] == "busy" for item in seen))
        self.assertEqual(len(self.store.list("runs")), 5)

    async def test_legacy_refresh_preserves_auto_audit_without_daily_block(self):
        row = self.existing_attempt_history()
        row.update(status="failed", manual_requested=True, attempts=3)
        self.store.put("reports", row)
        news_reports.retry_report(self.store, row["id"])
        self.assertEqual((await news_reports.process_report_queue(self.store, runner=successful_runner))["processed"], 1)
        self.assertEqual(news_reports.report_budget(self.store)["manual_used_today"], 0)
        self.assertEqual(news_reports.report_budget(self.store)["used_today"], 13)

    async def test_failed_manual_attempt_does_not_retry_after_config_or_refresh(self):
        news_reports.organize_reports(self.store, "arsenal")
        self.assertEqual((await news_reports.process_report_queue(self.store, runner=failure_runner))["failed"], 1)
        row = self.store.list("reports")[0]
        self.assertIsNone(row["retry_at"])
        self.assertFalse(row["manual_once"])
        self.store.config["llm"]["model"] = "reconfigured-model"
        news_reports.enqueue_reports(self.store)
        news_reports.retry_pending_reports(self.store, "arsenal")
        news_reports.retry_report(self.store, row["id"])
        news_reports.recover_reports(self.store)
        runner = AsyncMock(side_effect=successful_runner)
        self.assertEqual((await news_reports.process_report_queue(self.store, runner=runner))["processed"], 0)
        runner.assert_not_called()
        self.assertEqual(news_reports.organize_reports(self.store, "arsenal")["queued"], 1)
        self.assertEqual((await news_reports.process_report_queue(self.store, runner=runner))["completed"], 1)
        self.assertEqual(news_reports.report_budget(self.store)["manual_used_today"], 2)

    async def test_interrupted_manual_attempt_is_not_reauthorized_on_restart(self):
        news_reports.organize_reports(self.store, "arsenal")
        async def interrupted(store, run_id, cancel):
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await news_reports.process_report_queue(self.store, runner=interrupted)
        row = self.store.list("reports")[0]
        # Simulate a hard shutdown before the worker could mark the row failed.
        row.update(status="generating")
        self.store.put("reports", row)
        self.assertEqual(news_reports.recover_reports(self.store), 1)
        news_reports.enqueue_reports(self.store)
        self.assertEqual((await news_reports.process_report_queue(self.store, runner=successful_runner))["processed"], 0)
        self.assertIsNone(self.store.list("reports")[0]["retry_at"])
        self.assertEqual(news_reports.report_budget(self.store)["manual_used_today"], 1)

    async def test_manual_works_with_auto_disabled_and_uses_feed_role(self):
        self.store.config["reports"]["auto_generate"] = False
        settings = {**self.store.settings(), "llm": {**self.store.settings()["llm"], "model": "editorial-model"}}
        with patch("server.model_config.role_settings", return_value=settings), \
                patch("server.model_config.role_selection", side_effect=lambda store, role: {"provider_id": "editorial", "model": "editorial-model"} if role == "feed" else None):
            self.assertEqual(news_reports.organize_reports(self.store, "arsenal")["queued"], 1)
            self.assertEqual((await news_reports.process_report_queue(self.store, runner=successful_runner))["completed"], 1)
        run = self.store.list("runs")[0]
        self.assertEqual((run["provider_id"], run["model"], run["model_role"]), ("editorial", "editorial-model", "feed"))
        self.assertEqual(news_reports.report_budget(self.store)["used_today"], 0)

    async def test_new_manual_request_takes_priority_after_active_automatic_job(self):
        for number in range(4):
            event(self.store, str(number))
        news_reports.enqueue_reports(self.store)
        first = True
        async def request_during_automatic_job(store, run_id, cancel):
            nonlocal first
            if first:
                first = False
                request = news_reports.organize_reports(store, "arsenal")
                self.assertEqual(request["queued"], 4)
            await successful_runner(store, run_id, cancel)
        await news_reports.process_report_queue(self.store, runner=request_during_automatic_job)
        self.assertEqual([run["manual_report"] for run in self.store.list("runs")], [False, True, True, True, True])
        self.assertEqual(personal_app.feed_view(self.store, "arsenal")["processing"]["manual_count"], 0)

    async def test_manual_request_does_not_keep_authorizing_later_events(self):
        self.store.config["reports"]["auto_generate"] = False
        news_reports.organize_reports(self.store, "arsenal")
        event(self.store, "arrived_later")
        news_reports.enqueue_reports(self.store)
        result = await news_reports.process_report_queue(self.store, runner=successful_runner)
        self.assertEqual(result["completed"], 1)
        self.assertEqual({row["event_id"] for row in self.store.list("reports")}, {"event_one"})
        self.assertEqual((await news_reports.process_report_queue(self.store, runner=successful_runner))["processed"], 0)

    async def test_source_privacy_team_scope_and_completed_reports_are_preserved(self):
        source = self.store.get("evidence", "ev_injury")
        for flags in ({"private": True}, {"deleted": True}, {"visibility": "hidden"}, {"spoiler": True}, {"fixture_record": True}):
            self.store.put("evidence", {**source, **flags})
            self.assertEqual(news_reports.organize_reports(self.store, "arsenal")["status"], "idle", flags)
        self.store.put("evidence", source)
        foreign = event(self.store, "foreign")
        foreign.update(team_id="liverpool", evidence_ids=["ev_other_team"])
        self.store.put("events", foreign)
        news_reports.organize_reports(self.store, "arsenal")
        self.assertTrue(all(row["team_id"] == "arsenal" for row in self.store.list("reports")))
        await news_reports.process_report_queue(self.store, runner=successful_runner)
        completed = self.store.list("reports")[0]
        self.assertEqual(news_reports.organize_reports(self.store, "arsenal")["status"], "idle")
        self.assertEqual(self.store.get("reports", completed["id"]), completed)

    async def test_source_changes_while_queued_consume_no_model_and_keep_no_grant(self):
        news_reports.organize_reports(self.store, "arsenal")
        source = self.store.get("evidence", "ev_injury")
        source["private"] = True
        self.store.put("evidence", source)
        runner = AsyncMock(side_effect=successful_runner)
        self.assertEqual((await news_reports.process_report_queue(self.store, runner=runner))["processed"], 0)
        self.assertFalse(self.store.list("reports")[0]["manual_once"])
        runner.assert_not_called()

    async def test_unverified_manual_delivery_remains_unpublished(self):
        news_reports.organize_reports(self.store, "arsenal")
        async def unsupported(store, run_id, cancel):
            await successful_runner(store, run_id, cancel)
            run = store.get("runs", run_id)
            run["verification"] = {"status": "unsupported"}
            store.put("runs", run)
        await news_reports.process_report_queue(self.store, runner=unsupported)
        row = self.store.list("reports")[0]
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["report"])
        self.assertEqual(personal_app.feed_view(self.store, "arsenal")["items"], [])


class ManualReportAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.directory.name) / "manual.sqlite3")
        self.store = self.app.state.store
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")

    async def asyncTearDown(self):
        await self.app.state.personal.close()
        await self.app.state.team_maintenance.close()
        await self.client.aclose()
        self.store.close()
        self.directory.cleanup()

    async def test_configuration_errors_are_readable_and_team_is_required(self):
        response = await self.client.post("/api/feed/organize", json={"team_id": "arsenal"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("球队动态", response.json()["detail"])
        self.assertEqual((await self.client.post("/api/feed/organize", json={})).status_code, 422)
        self.assertEqual((await self.client.post("/api/feed/organize", json={"team_id": "unknown"})).status_code, 404)

    async def test_idle_and_queue_calls_do_not_start_network_or_model_inline(self):
        self.store.save_settings({"llm": {"base_url": "http://localhost:9999/v1", "model": "test"}})
        with patch("server.agent.run_agent", new=AsyncMock()) as model, \
                patch.object(self.app.state.personal, "start") as refresh:
            idle = await self.client.post("/api/feed/organize", json={"team_id": "arsenal"})
            self.assertEqual(idle.status_code, 200)
            self.assertEqual(idle.json()["status"], "idle")
            self.store.put("evidence", FakeStore().get("evidence", "ev_injury"))
            event(self.store)
            queued = await self.client.post("/api/feed/organize", json={"team_id": "arsenal"})
            self.assertEqual(set(queued.json()), {"started", "status", "queued", "note"})
            self.assertEqual((queued.json()["status"], queued.json()["queued"]), ("queued", 1))
            again = await self.client.post("/api/feed/organize", json={"team_id": "arsenal"})
            self.assertEqual(again.json()["status"], "busy")
            feed = (await self.client.get("/api/feed", params={"team_id": "arsenal"})).json()
            self.assertEqual(feed["processing"]["manual_count"], 1)
            model.assert_not_called()
            refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
