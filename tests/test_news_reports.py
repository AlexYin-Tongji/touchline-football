"""Durable report queue, source/privacy invalidation and real delivery protocol."""
import asyncio
import copy
import json
import unittest
import tempfile
from pathlib import Path
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from server import agent, news_reports, verification
from server.agent_tools import LocalTools, ToolError, tool_definitions, evidence_passages
from server.storage import Store
from tests.test_agent import FakeStore, tool_message


def manuscript():
    return {"status": "complete", "title": "中场暂缺训练，伤情评估与后续安排仍需关注",
            "summary": "当前收录的医疗消息提到，一名中场受伤，暂时无法参加训练。报道的重点在于训练可用性，比赛出场情况仍需更多材料。",
            "facts": [{"title": title, "kind": kind,
                       "detail": "收录的医疗更新提到，一名中场存在伤情，因而暂时无法参与训练。原文将受伤与无法参加训练联系起来，没有在这句话中给出恢复时间。",
                       "evidence_ids": ["ev_injury"], "quotes": [{"evidence_id": "ev_injury", "passage_id": evidence_passages("Medical update: the midfielder has an injury and is unavailable for training.")[0]["id"]}]}
                      for title, kind in [("医疗消息提到伤情", "development"), ("训练参与受到影响", "development"), ("这次更新说明了什么", "context")]],
            "analysis": "如果无法训练的情况继续，球队的准备可能受到影响；实际安排仍需后续信息。",
            "unknowns": ["后续训练参与情况", "恢复时间和比赛名单"]}


def event(store, suffix="one"):
    return store.put("events", {"id": "event_" + suffix, "team_id": "arsenal", "mode": "live",
                                "title": "Medical update", "summary": "", "evidence_ids": ["ev_injury"]})


async def successful_runner(store, run_id, cancel):
    run = store.get("runs", run_id)
    run.update(status="completed", structured_answer=manuscript(), evidence_ids=["ev_injury"],
               read_evidence_ids=["ev_injury"], verification={"status": "supported"})
    store.put("runs", run)


class NewsReportQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixing_checker_assignment_resumes_exhausted_automatic_job(self):
        roles = {"verify": {"provider_id": "checker", "model": "old"}}
        self.store.get_meta = lambda key: roles if key == "model_roles" else None
        self.store.get_secret = lambda key: "synthetic-checker-key"
        self.store.put("model_profiles", {"id": "checker", "base_url": "https://checker.example/v1",
                                         "protocol": "chat_completions", "models": [{"id": "old"}, {"id": "new"}]})
        row = news_reports.enqueue_reports(self.store)[0]
        self.store.put("reports", {**row, "status": "failed", "attempts": 3, "retry_at": None})
        news_reports.enqueue_reports(self.store)
        self.assertEqual(self.store.get("reports", row["id"])["status"], "failed")
        roles["verify"]["model"] = "new"
        news_reports.enqueue_reports(self.store)
        resumed = self.store.get("reports", row["id"])
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(resumed["attempts"], 0)
        self.assertNotIn("synthetic-checker-key", json.dumps(resumed))

    def setUp(self):
        self.store = FakeStore()
        self.store.config["reports"] = {"auto_generate": True, "max_per_cycle": 3}
        event(self.store)

    async def generate(self):
        row = news_reports.enqueue_reports(self.store)[0]
        await news_reports.process_report_queue(self.store, runner=successful_runner)
        return news_reports.get_report(self.store, row["id"])

    async def test_completed_report_is_separate_and_reads_never_call_model(self):
        original = copy.deepcopy(self.store.get("evidence", "ev_injury"))
        row = await self.generate()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(len(row["report"]["sections"]), 3)
        self.assertEqual(row["report"]["sources"][0]["id"], "ev_injury")
        self.assertEqual(self.store.get("evidence", "ev_injury"), original)
        with patch.object(agent, "run_agent", new=AsyncMock()) as model:
            for _ in range(4):
                self.assertEqual(news_reports.list_reports(self.store)[0]["status"], "completed")
                self.assertEqual(news_reports.get_report(self.store, row["id"])["status"], "completed")
            self.assertEqual(news_reports.enqueue_reports(self.store)[0]["id"], row["id"])
            self.assertEqual((await news_reports.process_report_queue(self.store))["processed"], 0)
        model.assert_not_called()

    async def test_existing_verified_report_hides_only_machine_annotations_without_regeneration(self):
        row = await self.generate()
        raw = self.store.get("reports", row["id"])
        original = "原文为可提取的部分正文（content_status=partial），可能存在未收录的更新信息。"
        factual_note = "球员接受评估（周三训练后），尚未公布复出时间。"
        raw["report"]["next_steps"] = [original, factual_note]
        raw["report"]["sources"][0]["title"] = "Source (content_status=partial)"
        self.store.put("reports", raw)
        with patch.object(agent, "run_agent", new=AsyncMock()) as model:
            clean = news_reports.get_report(self.store, row["id"])
        self.assertEqual(clean["report"]["next_steps"], ["原文为可提取的部分正文，可能存在未收录的更新信息。", factual_note])
        self.assertEqual(clean["report"]["sources"][0]["title"], "Source (content_status=partial)")
        self.assertEqual(self.store.get("reports", row["id"])["report"]["next_steps"][0], original)
        self.assertEqual(clean["verification_status"], "supported")
        model.assert_not_called()

    async def test_report_images_are_rechecked_for_redaction_and_source_privacy(self):
        source = self.store.get("evidence", "ev_injury")
        image = {"url": "https://cdn.example.com/team.jpg", "source_url": "https://example.com/news/training", "evidence_id": source["id"]}
        source.update(origin="web", url=image["source_url"], images=[image],
                      title="Medical update", spoiler=True,
                      text="Medical staff are assessing the midfielder's injury and will review availability before the next training session. " * 4 + "\n\nThe team won 2-1.")
        self.store.put("evidence", source)
        row = await self.generate()
        self.assertEqual(row["report"]["images"], [])
        # Legacy or modified report metadata cannot bypass the public boundary.
        stored = self.store.get("reports", row["id"])
        stored["report"]["images"] = [image]
        stored["report"]["sections"][0]["illustrations"] = [image]
        self.store.put("reports", stored)
        public = news_reports.get_report(self.store, row["id"])
        self.assertEqual(public["status"], "completed")
        self.assertEqual(public["report"]["images"], [])
        self.assertEqual(public["report"]["sections"][0]["illustrations"], [])
        for change in ({"private": True}, {"deleted": True}):
            self.store.put("evidence", {**source, **change})
            public = news_reports.get_report(self.store, row["id"])
            self.assertIsNone(public["report"])
            self.assertNotIn("images", public)

    async def test_source_edits_deletion_and_preferences_invalidate_without_leaking_old_prose(self):
        row = await self.generate()
        original = self.store.get("evidence", "ev_injury")
        for change in ({"text": "Corrected source body with different details."}, {"private": True}, {"spoiler": True}):
            self.store.put("evidence", {**original, **change})
            current = news_reports.get_report(self.store, row["id"])
            self.assertEqual(current["status"], "stale")
            self.assertIsNone(current["report"])
            if change.get("private"):
                self.assertEqual(current["source_excerpt"], "")
            self.store.put("evidence", original)
        self.store.config["personalization"] = {"fan_level": "beginner"}
        self.store.config["explanation_depth"] = "brief"
        self.assertEqual(news_reports.get_report(self.store, row["id"])["status"], "completed")
        replacement = news_reports.enqueue_reports(self.store)[0]
        self.assertEqual(replacement["id"], row["id"])
        self.store.data["evidence"].pop("ev_injury")
        self.assertEqual(news_reports.get_report(self.store, replacement["id"])["source_excerpt"], "")

    async def test_configuration_state_can_resume_and_auto_off_does_not_run(self):
        self.store.config["llm"]["model"] = ""
        row = news_reports.enqueue_reports(self.store)[0]
        self.assertEqual(row["status"], "needs_configuration")
        runner = AsyncMock(side_effect=successful_runner)
        self.assertEqual((await news_reports.process_report_queue(self.store, runner=runner))["processed"], 0)
        self.store.config["llm"]["model"] = "test"
        self.store.config["reports"]["auto_generate"] = False
        self.assertEqual((await news_reports.process_report_queue(self.store, runner=runner))["processed"], 0)
        news_reports.retry_report(self.store, row["id"])
        self.assertEqual((await news_reports.process_report_queue(self.store, runner=runner))["completed"], 1)

    async def test_failure_retries_require_clicks_and_attempt_history_is_not_a_quota(self):
        row = news_reports.enqueue_reports(self.store)[0]
        async def failure(store, rid, cancel):
            store.put("runs", {**store.get("runs", rid), "status": "failed", "error": "测试中的模型暂不可用"})
        for attempt in range(3):
            if attempt:
                news_reports.retry_report(self.store, row["id"])
            await news_reports.process_report_queue(self.store, runner=failure)
        failed = news_reports.get_report(self.store, row["id"])
        self.assertEqual(len(self.store.get("reports", row["id"])["attempt_history"]), 3)
        self.assertIsNone(failed["retry_at"])
        self.assertEqual((await news_reports.process_report_queue(self.store, runner=failure))["processed"], 0)
        raw = self.store.get("reports", row["id"])
        raw["attempt_history"] = [news_reports.now().isoformat()] * 12
        self.store.put("reports", raw)
        queued = news_reports.retry_report(self.store, row["id"])
        self.assertIsNone(queued["budget"]["remaining_today"])
        self.assertNotIn("额度", queued["queue_note"])
        self.assertEqual((await news_reports.process_report_queue(self.store, runner=successful_runner))["processed"], 1)
        self.assertEqual(news_reports.report_budget(self.store)["used_today"], 13)

    async def test_restart_adopts_verified_run_without_second_model_call(self):
        row = news_reports.enqueue_reports(self.store)[0]
        row.update(status="generating", run_id="saved_run", attempts=1)
        self.store.put("reports", row)
        self.store.put("runs", {"id": "saved_run", "team_id": "arsenal", "mode": "live"})
        await successful_runner(self.store, "saved_run", asyncio.Event())
        self.assertEqual(news_reports.recover_reports(self.store), 1)
        self.assertEqual(news_reports.get_report(self.store, row["id"])["status"], "completed")

    async def test_mutation_during_model_request_cannot_publish_old_report(self):
        row = news_reports.enqueue_reports(self.store)[0]
        async def changing_runner(store, rid, cancel):
            await successful_runner(store, rid, cancel)
            source = store.get("evidence", "ev_injury")
            source["text"] = "This source was corrected while the report was written."
            store.put("evidence", source)
        await news_reports.process_report_queue(self.store, runner=changing_runner)
        result = news_reports.get_report(self.store, row["id"])
        self.assertEqual(result["status"], "stale")
        self.assertIsNone(result["report"])

    async def test_workers_do_not_overlap_and_default_cycle_drains_all_due_work(self):
        for suffix in ("two", "three", "four", "five"):
            event(self.store, suffix)
        news_reports.enqueue_reports(self.store)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_runner(store, rid, cancel):
            entered.set()
            await release.wait()
            await successful_runner(store, rid, cancel)
        first = asyncio.create_task(news_reports.process_report_queue(self.store, runner=slow_runner))
        await entered.wait()
        self.assertTrue((await news_reports.process_report_queue(self.store, runner=successful_runner))["busy"])
        release.set()
        self.assertEqual((await first)["processed"], 5)

    async def test_auto_queue_has_no_backfill_cycle_or_daily_quota(self):
        # Old settings must not silently reinstate a three-report ceiling.
        self.store.config["reports"]["max_per_cycle"] = 1
        self.store.data["events"] = {}
        for index in range(105):
            event(self.store, str(index))
        rows = news_reports.enqueue_reports(self.store)
        self.assertEqual(len(rows), 105)
        result = await news_reports.process_report_queue(self.store, runner=successful_runner)
        self.assertEqual(result["completed"], 105)
        self.assertEqual(result["budget"]["used_today"], 105)
        self.assertIsNone(result["budget"]["daily_limit"])

    async def test_all_event_sources_are_available_without_invalidating_old_fingerprint(self):
        source = self.store.get("evidence", "ev_injury")
        ids = [source["id"]]
        for index in range(6):
            extra = self.store.put("evidence", {**source, "id": f"ev_extra_{index}"})
            ids.append(extra["id"])
        current = self.store.get("events", "event_one")
        current["evidence_ids"] = ids
        self.store.put("events", current)
        snapshot = news_reports._snapshot(self.store, current, self.store.settings())
        old_fingerprint = news_reports._hash({"version": 2, "team_id": current["team_id"], "mode": current["mode"],
            "event_id": current["id"], "sources": snapshot["source_hashes"],
            "visible": {item["id"]: news_reports._evidence_hash(item) for item in snapshot["sources"][:5]},
            "boundary": news_reports._boundary(self.store.settings())})
        self.assertEqual(snapshot["fingerprint"], old_fingerprint)
        row = news_reports.enqueue_reports(self.store)[0]
        self.assertEqual(set(row["evidence_ids"]), set(ids))
        await news_reports.process_report_queue(self.store, runner=successful_runner)
        self.assertEqual(set(self.store.list("runs")[0]["input_evidence_ids"]), set(ids))
        # A completed report created by the old five-source version stays valid.
        old = self.store.get("reports", row["id"])
        old.update(fingerprint=old_fingerprint, evidence_ids=ids[:5], source_count=5)
        self.store.put("reports", old)
        self.assertEqual(news_reports.get_report(self.store, old["id"])["status"], "completed")
        self.assertEqual(news_reports.enqueue_reports(self.store)[0]["id"], old["id"])
        self.assertEqual(len(self.store.list("reports")), 1)

    async def test_verified_report_can_have_more_than_six_supported_sections(self):
        news_reports.enqueue_reports(self.store)
        async def detailed_runner(store, run_id, cancel):
            await successful_runner(store, run_id, cancel)
            run = store.get("runs", run_id)
            fact = run["structured_answer"]["facts"][0]
            run["structured_answer"]["facts"] = [{**fact, "title": f"原文支持的细节 {index}"} for index in range(8)]
            store.put("runs", run)
        result = await news_reports.process_report_queue(self.store, runner=detailed_runner)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(len(self.store.list("reports")[0]["report"]["sections"]), 8)

    async def test_backfill_limit_counts_missing_jobs_after_deduplication(self):
        self.store.data["events"] = {}
        for index in range(6):
            item = event(self.store, str(index))
            item["updated_at"] = f"2026-09-0{index + 1}T10:00:00Z"
            self.store.put("events", item)
        first = news_reports.enqueue_reports(self.store, limit=2)
        self.assertEqual({row["event_id"] for row in first}, {"event_5", "event_4"})
        await news_reports.process_report_queue(self.store, max_jobs=2, runner=successful_runner)
        second = news_reports.enqueue_reports(self.store, limit=2)
        self.assertEqual({row["event_id"] for row in second}, {"event_3", "event_2"})
        third = news_reports.enqueue_reports(self.store, limit=2)
        self.assertEqual({row["event_id"] for row in third}, {"event_1", "event_0"})
        self.assertEqual(len(self.store.list("reports")), 6)

    async def test_fixture_only_events_do_not_consume_news_backfill_or_model_budget(self):
        self.store.put("evidence", {**self.store.get("evidence", "ev_injury"), "id": "ev_fixture",
                                    "fixture_record": True, "title": "Next fixture", "text": "Arsenal vs Test City, upcoming."})
        self.store.put("events", {"id": "event_fixture", "mode": "live", "team_id": "arsenal",
                                  "updated_at": "2099-01-01T00:00:00Z", "evidence_ids": ["ev_fixture"]})
        rows = news_reports.enqueue_reports(self.store, limit=1)
        self.assertEqual([row["event_id"] for row in rows], ["event_one"])
        await news_reports.process_report_queue(self.store, runner=successful_runner)
        self.assertEqual(news_reports.report_budget(self.store)["used_today"], 1)
        self.assertEqual(len(self.store.list("reports")), 1)

    async def test_sqlite_restart_preserves_pending_jobs_and_attempt_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reports.sqlite3"
            stored = Store(path)
            stored.save_settings(self.store.settings())
            for collection in ("teams", "events", "evidence"):
                for value in self.store.list(collection):
                    stored.put(collection, value)
            row = news_reports.enqueue_reports(stored)[0]
            stored.close()
            reopened = Store(path)
            self.assertEqual(news_reports.get_report(reopened, row["id"])["status"], "queued")
            await news_reports.process_report_queue(reopened, runner=successful_runner)
            run = reopened.get("runs", reopened.get("reports", row["id"])["run_id"])
            self.assertTrue(run["internal_report"])
            self.assertTrue(run["conversation_id"])
            reopened.close()
            verified = Store(path)
            self.assertEqual(news_reports.get_report(verified, row["id"])["status"], "completed")
            self.assertEqual(news_reports.report_budget(verified)["used_today"], 1)
            self.assertEqual(len(verified.list("reports")), 1)
            verified.close()

    async def test_shutdown_stops_worker_even_if_agent_persists_and_swallows_cancellation(self):
        event(self.store, "two")
        news_reports.enqueue_reports(self.store)
        entered = asyncio.Event()
        async def cancellation_saving_agent(store, rid, cancel):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                store.put("runs", {**store.get("runs", rid), "status": "cancelled"})
        worker = asyncio.create_task(news_reports.process_report_queue(self.store, runner=cancellation_saving_agent))
        await entered.wait()
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker
        self.assertEqual(len(self.store.list("runs")), 2)
        self.assertFalse(any(row["status"] == "generating" for row in self.store.list("reports")))
        self.assertFalse(news_reports._worker_lock(self.store).locked())


class NewsReportProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeStore()
        self.run = self.store.new_run(skill="news_report", report_id="report_one", input_evidence_ids=["ev_injury"], allow_network=False)
        self.local = LocalTools(self.store, self.run, self.store.settings())
        self.local.call("read_evidence", {"evidence_id": "ev_injury"})

    def test_report_and_normal_schema_allow_needed_detail_without_relaxing_source_scope(self):
        value = manuscript()
        value["facts"][0]["detail"] *= 5
        value["analysis"] *= 9
        self.local.call("submit_answer", value)
        normal = next(item for item in tool_definitions() if item["name"] == "submit_answer")["parameters"]["properties"]
        self.assertNotIn("maxItems", normal["facts"])
        self.assertGreaterEqual(normal["analysis"]["maxLength"], 16000)
        self.assertTrue(self.local.verification_enabled)
        with self.assertRaises(ToolError):
            self.local.call("read_evidence", {"evidence_id": "ev_transfer"})

    def test_all_report_prose_including_title_is_checked_against_actual_passages(self):
        value = manuscript()
        value["facts"].extend([{**copy.deepcopy(value["facts"][0]), "title": "正文细节" + str(i)} for i in range(3)])
        self.local.call("submit_answer", value)
        public, payload = verification.prepare_claims(self.local, self.local.submission["structured_answer"])
        self.assertEqual({claim["id"] for claim in public}, {"title", "summary", "analysis", "unknowns", *["fact_" + str(i) for i in range(1, 7)]})
        self.assertTrue(payload)
        invalid = manuscript()
        invalid["facts"][0]["quotes"] = [{"evidence_id": "ev_injury", "passage_id": "unread_and_invented_passage"}]
        self.local.call("submit_answer", invalid)
        public, _ = verification.prepare_claims(self.local, self.local.submission["structured_answer"])
        self.assertEqual(public[0]["verdict"], "unsupported")

    async def test_real_harness_verifies_model_chosen_report_delivery_once(self):
        self.store.config["llm"]["max_output_tokens"] = 16000
        self.store.config["llm"]["timeout_seconds"] = 37
        def checked(config, messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            return tool_message("report_source_support", {"claims": [{"id": item["id"], "verdict": "supported", "reason": "对应所引原文中的训练可用性说明。"} for item in payload["claims"]]})
        responses = [tool_message("submit_answer", manuscript())]
        async def requester(config, messages, **kwargs):
            choice = kwargs.get("tool_choice")
            function = choice.get("function", {}).get("name") if isinstance(choice, dict) else None
            if function == "report_source_support":
                self.assertEqual(kwargs["timeout"], 120)
                return checked(config, messages, **kwargs)
            self.assertNotEqual(function, "submit_answer")
            self.assertEqual(kwargs["timeout"], 37)
            self.assertEqual(config["max_output_tokens"], 16000)
            schema = next(item for item in kwargs["tools"] if item["function"]["name"] == "submit_answer")
            self.assertNotIn("maxItems", schema["function"]["parameters"]["properties"]["facts"])
            self.assertNotIn("quote", schema["function"]["parameters"]["properties"]["facts"]["items"]["properties"]["quotes"]["items"]["properties"])
            return responses.pop(0)
        with patch.object(agent, "request_completion", new=AsyncMock(side_effect=requester)) as provider:
            await agent.run_agent(self.store, "run_one", asyncio.Event())
        run = self.store.get("runs", "run_one")
        self.assertEqual(run["status"], "completed", run.get("error"))
        self.assertEqual(run["verification"]["status"], "supported")
        self.assertEqual(run["progress"]["time_limit_seconds"], agent.TASK_TIMEOUT_SECONDS)
        self.assertEqual(self.store.settings()["llm"]["timeout_seconds"], 37)
        self.assertEqual(provider.await_count, 2)
        self.assertEqual(run["structured_answer"]["title"], manuscript()["title"])


if __name__ == "__main__":
    unittest.main()
