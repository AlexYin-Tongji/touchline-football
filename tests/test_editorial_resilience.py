"""Transient transport recovery and multilingual source support regressions."""
import asyncio
import json
import unittest
from unittest.mock import patch, AsyncMock

import httpx

from server import provider, verification, news_reports, agent
from tests.test_agent import FakeStore
from tests.test_news_reports import event
from server.agent_tools import LocalTools, ToolError, openai_tools, clean_delivery_text
from server import team_center
from tests.test_verification import quoted_submission, checker_result
from tests.test_agent import tool_message


class ModelTransportRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_exhausted_transport_attempts_are_counted_in_failed_run(self):
        client = httpx.AsyncClient; wire = []
        def handler(request):
            wire.append(request)
            return httpx.Response(503, json={"error": {"code": "overloaded"}})
        store = FakeStore(); run = store.new_run()
        with patch.object(provider.httpx, "AsyncClient", side_effect=lambda **kw: client(transport=httpx.MockTransport(handler), **kw)):
            await agent.run_agent(store, run["id"], asyncio.Event())
        finished = store.get("runs", run["id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(len(wire), 3)
        self.assertEqual(finished["metrics"].get("draft_http_attempts"), 3)
        self.assertEqual(finished["metrics"].get("transport_retries"), 2)

    async def request(self, handler):
        client = httpx.AsyncClient
        with patch.object(provider.httpx, "AsyncClient", side_effect=lambda **kw: client(transport=httpx.MockTransport(handler), **kw)):
            return await provider.request_completion({"base_url": "https://model.example/v1", "model": "test"}, [{"role": "user", "content": "check"}])

    async def test_connection_reset_recovers_same_generation_without_replaying_tools(self):
        attempts = []
        def handler(request):
            attempts.append(json.loads(request.content))
            if len(attempts) == 1:
                raise httpx.ConnectError("connection reset", request=request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "complete"}, "finish_reason": "stop"}]})
        result = await self.request(handler)
        self.assertEqual(result["content"], "complete")
        self.assertEqual(attempts, [attempts[0], attempts[0]])

    async def test_transient_gateway_error_recovers_but_auth_and_quota_do_not_retry(self):
        for status, code, expected in ((503, "overloaded", 2), (401, "invalid_api_key", 1), (429, "insufficient_quota", 1)):
            requests = []
            def handler(request):
                requests.append(request)
                if len(requests) == 1:
                    return httpx.Response(status, json={"error": {"code": code}})
                return httpx.Response(200, json={"choices": [{"message": {"content": "complete"}}]})
            if expected == 2:
                self.assertEqual((await self.request(handler))["content"], "complete")
            else:
                with self.assertRaises(provider.AgentFailure):
                    await self.request(handler)
            self.assertEqual(len(requests), expected)

    async def test_cancellation_during_retry_never_sends_another_request(self):
        seen = asyncio.Event(); calls = []
        def handler(request):
            calls.append(request); seen.set()
            raise httpx.ConnectError("reset", request=request)
        task = asyncio.create_task(self.request(handler))
        await seen.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(len(calls), 1)


class EditorialStageTests(unittest.TestCase):
    def test_news_cannot_invent_calendar_year_from_relative_season(self):
        store=FakeStore();run=store.new_run(skill="news_report");local=LocalTools(store,run,store.settings())
        source=store.get("evidence","ev_injury")
        source.update(text="Chelsea have allowed more shots since the start of last season. The article compares their current defence.")
        store.put("evidence",source);local.call("read_evidence",{"evidence_id":"ev_injury"})
        value=quoted_submission();value["facts"][0].update(title="防守",detail="切尔西自2023-24赛季开始以来被更多射门。",quotes=[{"evidence_id":"ev_injury","quote":source["text"]}])
        claims,_=verification.prepare_claims(local,value)
        self.assertEqual(claims[0]["verdict"],"unsupported")
        self.assertIn("2023",claims[0]["reason"])

    def test_numbered_major_finals_are_valid_explicit_stage_evidence(self):
        self.assertEqual(verification.missing_stage_support("三次打进重大赛事决赛", "France reached three major finals under their coach."), [])
        self.assertEqual(verification.missing_stage_support("打进决赛", "They reached the final."), [])
        self.assertEqual(verification.missing_stage_support("决赛", "The opening game is on Friday."), ["决赛"])
        self.assertEqual(verification.missing_stage_support("欧洲决赛", "The score was 3-0 during a European final."), [])

    def test_european_and_south_american_explicit_stage_terms(self):
        for title, text in (("半决赛", "La semifinal se juega mañana."), ("半决赛", "Nas semifinais da Copa."), ("小组赛", "La fase de grupos.")):
            self.assertEqual(verification.missing_stage_support(title, text), [])

    def test_news_wire_shape_keeps_substantive_body_without_template_fields(self):
        schema=next(t["function"]["parameters"] for t in openai_tools("news_report") if t["function"]["name"]=="submit_answer")
        self.assertNotIn("analysis",schema["properties"])
        self.assertNotIn("unknowns",schema["required"])
        self.assertIn("facts",schema["required"])
        store=FakeStore(); local=LocalTools(store,store.new_run(skill="news_report"),store.settings())
        local.call("read_evidence",{"evidence_id":"ev_injury"})
        from tests.test_news_reports import manuscript
        value=manuscript();value["summary"]='据报道，\\"据了解\\"只是限定说法。'
        value.pop("analysis");value.pop("unknowns")
        local.call("submit_answer",value)
        self.assertEqual(local.submission["structured_answer"]["summary"], '据报道，"据了解"只是限定说法。')


class CompleteNewsReadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_news_reads_last_page_before_first_model_request(self):
        store=FakeStore(); run=store.new_run(skill="news_report", input_evidence_ids=["ev_injury"])
        source=store.get("evidence","ev_injury")
        source.update(text=("The club reported training details. "*400)+"\n\nThe closing section concerns relegation candidates and academy values.")
        store.put("evidence",source); seen=[]
        async def request(config,messages,**options):
            seen.extend(messages)
            raise provider.AgentFailure("stop after inspecting actual read context")
        with patch.object(agent,"request_completion",new=request):
            await agent.run_agent(store,run["id"],asyncio.Event())
        self.assertTrue("closing section concerns relegation" in json.dumps(seen))


class CheckerProtocolRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_checker_entries_retry_same_complete_evidence_once(self):
        store=FakeStore(); local=LocalTools(store,store.new_run(),store.settings())
        local.call("read_evidence", {"evidence_id":"ev_injury"})
        requests=[]
        async def requester(config,messages,**options):
            requests.append((messages,options))
            return tool_message("report_source_support", {"claims":[]}) if len(requests)==1 else checker_result()
        result=await verification.verify_submission(local,quoted_submission(),{},requester=requester)
        self.assertEqual(result["status"],"supported")
        self.assertEqual(len(requests),2)
        self.assertEqual(requests[0][0][1],requests[1][0][1])
        self.assertEqual(result.get("protocol_repairs"),1)

    async def test_repeated_bad_format_never_certifies_any_claim(self):
        store=FakeStore();local=LocalTools(store,store.new_run(),store.settings())
        local.call("read_evidence",{"evidence_id":"ev_injury"})
        requester=AsyncMock(return_value=tool_message("report_source_support",{"claims":[]}))
        result=await verification.verify_submission(local,quoted_submission(),{},requester=requester)
        self.assertEqual(result["status"],"unavailable")
        self.assertFalse(any(x["verdict"]=="supported" for x in result["claims"]))
        self.assertEqual(requester.await_count,2)


class ManualReportRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_near_deadline_keeps_valid_news_instead_of_starting_advisory_check(self):
        from tests.test_news_reports import manuscript
        store=FakeStore();event(store);row=news_reports.enqueue_reports(store)[0]
        run=store.new_run(skill="news_report",report_id=row["id"],input_evidence_ids=["ev_injury"])
        async def request(config,messages,**options):
            if isinstance(options.get("tool_choice"),dict):
                await asyncio.sleep(2)
                raise provider.AgentFailure("late checker")
            return tool_message("submit_answer",manuscript())
        with patch.object(agent,"TASK_TIMEOUT_SECONDS",.2), patch.object(agent,"request_completion",new=request):
            await agent.run_agent(store,run["id"],asyncio.Event())
        self.assertEqual(store.get("runs",run["id"])["status"],"completed")
        self.assertEqual(len(store.get("runs",run["id"])["structured_answer"]["facts"]),3)

    async def test_checker_outage_preserves_complete_news_and_adds_one_review_notice(self):
        from tests.test_news_reports import manuscript
        store=FakeStore();event(store);row=news_reports.enqueue_reports(store)[0]
        run=store.new_run(skill="news_report",report_id=row["id"],input_evidence_ids=["ev_injury"])
        async def request(config,messages,**options):
            if options.get("tool_choice") != "auto" and isinstance(options.get("tool_choice"),dict):
                raise provider.AgentFailure("核验服务临时不可用")
            return tool_message("submit_answer",manuscript())
        with patch.object(agent,"request_completion",new=request):
            await agent.run_agent(store,run["id"],asyncio.Event())
        result=store.get("runs",run["id"])
        self.assertEqual(result["status"],"completed")
        self.assertEqual(len(result["structured_answer"]["facts"]),3)
        published=news_reports._finish(store,row,result,store.settings())
        self.assertEqual(published["status"],"completed")
        self.assertEqual(published["report"]["review_status"],"unavailable")
        self.assertTrue(published["report"]["review_notice"])
        self.assertEqual(len(published["report"]["sections"]),3)

    async def test_failed_article_waits_for_manual_retry_even_if_legacy_due_time_has_passed(self):
        store = FakeStore(); event(store)
        row = news_reports.enqueue_reports(store)[0]
        async def fail(store, rid, cancel):
            store.put("runs", {**store.get("runs", rid), "status": "failed", "error": "temporary outage"})
        await news_reports.process_report_queue(store, runner=fail)
        self.assertIsNone(store.get("reports", row["id"])["retry_at"])
        store.put("reports", {**store.get("reports", row["id"]), "retry_at": "2000-01-01T00:00:00Z"})
        self.assertEqual((await news_reports.process_report_queue(store, runner=fail))["processed"], 0)
        state = news_reports.queue_summary(store)
        self.assertEqual(state["retrying"], 0)
        self.assertIsNone(state["next_retry_at"])
        news_reports.retry_report(store, row["id"])
        self.assertEqual((await news_reports.process_report_queue(store, runner=fail))["processed"], 1)


class PlayerPhotoToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_tool_delivers_identity_checked_results_for_current_team(self):
        store = FakeStore(); run = store.new_run(); local = LocalTools(store, run, store.settings())
        expected = {"team_id": "arsenal", "status": "completed", "photos": [{"provider_id": "123", "name": "Player", "photo_url": "https://images.example/player.png", "photo_source_url": "https://team.example/player/123"}]}
        with patch.object(team_center, "fetch_player_photos", new=AsyncMock(return_value=expected)):
            result = await local.acall("fetch_player_photos", {"player_ids": ["123"]})
        self.assertEqual(result, expected)

    async def test_photo_tool_is_not_exposed_or_executable_outside_network_scope(self):
        store = FakeStore()
        for change in ({"allow_network": False}, {"report_id": "fixed_article"}, {"mode": "demo"}):
            local = LocalTools(store, {**store.new_run(), **change}, store.settings())
            self.assertNotIn("fetch_player_photos", {v["function"]["name"] for v in local.available_tools()})
            with self.assertRaises(ToolError):
                await local.acall("fetch_player_photos", {})
