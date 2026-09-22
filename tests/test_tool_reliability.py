"""Regression cases from tool failures; all sources and providers are local mocks."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from server import agent, verification
from server.agent_tools import LocalTools, ToolError, TOOL_DEFINITIONS
from server.team_profiles import _evidence_hash
from tests.test_agent import FakeStore, tool_message
from tests.test_agent_delivery import submission
from tests.test_verification import quoted_submission, checker_result


class ToolContractTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.run = self.store.new_run()
        self.local = LocalTools(self.store, self.run, self.store.settings())

    def test_nested_validation_names_repairable_field_and_limit(self):
        value = submission()
        value["facts"][0]["detail"] = "字" * 12001
        with self.assertRaisesRegex(ToolError, r"arguments\.facts\[0\]\.detail.*12000"):
            self.local.call("submit_answer", value)
        value = submission(unknowns=["a" * 2001])
        with self.assertRaisesRegex(ToolError, r"arguments\.unknowns\[0\].*2000"):
            self.local.call("submit_answer", value)
        value = submission()
        value["private-secret-value"] = "never echo this"
        with self.assertRaises(ToolError) as caught:
            self.local.call("submit_answer", value)
        self.assertNotIn("private-secret-value", str(caught.exception))
        self.assertNotIn("never echo", str(caught.exception))

    def test_only_executable_tools_are_advertised_for_each_scope(self):
        def names(local):
            return {item["function"]["name"] for item in local.available_tools()}
        self.assertTrue({"research_news", "get_team_data", "conversation_history", "search_team_knowledge"} <= names(self.local))
        preview = LocalTools(self.store, dict(self.run, skill="match_preview", match_id="match_one"), self.store.settings())
        self.assertFalse({"get_team_data", "maintain_team_data", "discover_news", "read_article", "research_news"} & names(preview))
        self.assertIn("match_context", names(preview))
        report = LocalTools(self.store, dict(self.run, skill="news_report", report_id="r", input_evidence_ids=["ev_injury"]), self.store.settings())
        self.assertFalse({"get_team_data", "maintain_team_data", "research_news", "conversation_history", "search_team_knowledge"} & names(report))
        self.assertTrue({"read_evidence", "submit_answer", "compare_sources"} <= names(report))
        self.local.network_queries = 30
        self.local.article_reads = 80
        self.assertTrue({"research_news", "read_article"} <= names(self.local))
        self.assertEqual(len({item["name"] for item in TOOL_DEFINITIONS}), len(TOOL_DEFINITIONS))

    def test_history_isolated_by_conversation_role_match_and_current_source_visibility(self):
        base = dict(self.run, status="completed", answer="safe answer", evidence_ids=["ev_injury"])
        self.store.put("runs", dict(base, id="old", created_at="2026-09-01"))
        for key, changes in {
            "different_conversation": {"conversation_id": "other"},
            "background": {"internal_report": True}, "feed": {"model_role": "feed"},
            "match": {"match_id": "match_one"}, "deleted": {"evidence_ids": ["missing"]},
            "private": {"evidence_ids": ["private-source"]}, "other_team": {"team_id": "liverpool"},
        }.items():
            self.store.put("runs", dict(base, id=key, **changes))
        self.store.put("evidence", dict(self.store.get("evidence", "ev_injury"), id="private-source", private=True))
        # Source privacy is enforced even when the user has disabled spoilers.
        settings = dict(self.store.settings(), spoiler_free=False)
        local = LocalTools(self.store, self.run, settings)
        result = local.call("conversation_history", {})
        self.assertEqual([row["run_id"] for row in result["messages"]], ["old"])
        self.assertEqual(len(agent.history_messages(self.store, self.run, local)), 2)
        self.assertFalse(local.read_ids)

    def test_history_keeps_early_messages_and_full_answers_with_optional_pagination(self):
        base = dict(self.run, status="completed", answer="较长的历史回答。" * 500,
                    question="需要保留的早期问题" * 200, evidence_ids=["ev_injury"])
        for index in range(8):
            self.store.put("runs", dict(base, id=f"old_{index}", created_at=f"2026-09-{index + 1:02d}"))
        full = self.local.call("conversation_history", {})
        self.assertEqual(len(full["messages"]), 8)
        self.assertIsNone(full["next_offset"])
        self.assertEqual(full["messages"][0]["answer"], base["answer"])
        self.assertEqual(full["messages"][0]["question"], base["question"])
        messages = agent.history_messages(self.store, self.run, self.local)
        self.assertEqual(len(messages), 16)
        self.assertIn(base["answer"], messages[1]["content"])
        first = self.local.call("conversation_history", {"limit": 3})
        second = self.local.call("conversation_history", {"offset": first["next_offset"], "limit": 3})
        final = self.local.call("conversation_history", {"offset": second["next_offset"], "limit": 3})
        self.assertEqual([row["run_id"] for page in (first, second, final) for row in page["messages"]],
                         [f"old_{index}" for index in range(8)])
        self.assertIsNone(final["next_offset"])

    def test_parent_pointer_cannot_import_background_or_other_role_context(self):
        for changes in ({"internal_report": True}, {"report_id": "report_one"}, {"model_role": "team"}):
            self.store.put("runs", dict(self.run, id="parent", status="completed", answer="background context", **changes))
            run = dict(self.run, parent_run_id="parent")
            local = LocalTools(self.store, run, self.store.settings())
            with self.assertRaisesRegex(agent.AgentFailure, "其他功能"):
                agent.resume_context(self.store, run, local)
            self.assertFalse(local.read_ids)

    def test_knowledge_search_returns_current_scoped_leads_without_granting_citations(self):
        source = self.store.get("evidence", "ev_injury")
        knowledge = {"id": "knowledge_one", "mode": "live", "team_id": "arsenal", "title": "训练状态", "detail": "中场仍缺席训练。",
                     "evidence_ids": [source["id"]], "sources": [{"id": source["id"], "content_hash": _evidence_hash(source)}],
                     "support": "source_supported"}
        self.store.put("knowledge", knowledge)
        self.store.put("knowledge", dict(knowledge, id="stale", sources=[]))
        found = self.local.call("search_team_knowledge", {"query": "训练"})
        self.assertEqual([row["id"] for row in found["results"]], ["knowledge_one"])
        self.assertFalse(self.local.read_ids)
        self.store.put("evidence", dict(source, private=True))
        self.assertFalse(self.local.call("search_team_knowledge", {"query": "训练"})["results"])

    def test_escaped_chinese_submission_has_same_limits_as_literal_json(self):
        self.local.call("read_evidence", {"evidence_id": "ev_injury"})
        value = submission()
        # Legal long original quotes can be unicode escaped by a provider.
        quote = "这是一段用于参数序列化的本地测试原文。" * 30
        value["facts"][0]["quotes"] = [{"evidence_id": "ev_injury", "quote": quote} for _ in range(3)]
        call = tool_message("submit_answer", value)["tool_calls"][0]
        self.assertGreater(len(call["function"]["arguments"]), 10000)
        self.assertTrue(self.local.run_tool_call(call)["submitted"])

    def test_team_snapshot_keeps_identity_stats_and_distinct_times_in_one_citable_row(self):
        roster_time = "2026-09-16T03:24:45+00:00"
        latest_time = "2026-09-16T03:24:47+00:00"
        player = {"provider_id": "217092", "name": "Gianluigi Donnarumma", "number": "1", "position": "门将", "position_group": "GK",
                  "nationality": "意大利", "age": 27, "age_as_of": "2026-09-16", "height_cm": 196, "weight_kg": 88.9,
                  "stats": {"appearances": 4, "saves": 10, "goals_conceded": 2}, "bio": "irrelevant long profile " * 1000}
        payload = {"season": {"label": "2026/27"}, "roster": [player, *[dict(player, name=f"Other player {i}", provider_id=str(1000 + i)) for i in range(39)]],
                   "stats": {}, "roster_updated_at": roster_time, "updated_at": latest_time,
                   "sources": [{"name": "ESPN 阵容", "url": "https://www.espn.com/soccer/team/squad/_/id/382", "updated_at": roster_time}]}
        with patch("server.team_center.team_context_payload", return_value=payload):
            output = self.local.call("get_team_data", {})
        self.assertNotIn("roster", output, "unpaginated data cannot bypass the read-evidence boundary")
        eid = output["evidence"]["id"]
        first_page = self.local.call("read_evidence", {"evidence_id": eid, "length": 6000})
        passage = next(p for p in first_page["passages"] if "阵容第1行" in p["text"])
        for value in (player["name"], "号码=1", "位置=门将", "出场:4", "扑救:10", "阵容快照采集时间=" + roster_time, "资料总体最新采集时间=" + latest_time):
            self.assertIn(value, passage["text"])
        self.assertLessEqual(len(passage["text"]), 700)
        self.assertIn("ESPN 阵容", first_page["text"][:1000])
        self.assertNotIn("irrelevant long profile", first_page["text"])
        candidate = submission(facts=[{"title": "保存名单中的首条门将记录", "detail": "保存的名单第一行是Gianluigi Donnarumma，球衣号码1号，阵容记录采集于2026-09-16T03:24:45+00:00。",
                                      "evidence_ids": [eid], "quotes": [{"evidence_id": eid, "passage_id": passage["id"]}]}])
        self.local.call("submit_answer", candidate)
        claims, support = verification.prepare_claims(self.local, self.local.submission["structured_answer"])
        self.assertIsNotNone(support)
        self.assertEqual(len(claims[0]["quotes"]), 1)
        self.assertIn(roster_time, claims[0]["quotes"][0]["quote"])

    def test_missing_snapshot_timestamps_and_stats_stay_unknown(self):
        payload = {"season": {"label": "2026/27"}, "roster": [{"name": "Local player", "number": "3", "position": "后卫", "stats": {"appearances": None, "goals": 0}}],
                   "stats": {}, "sources": []}
        with patch("server.team_center.team_context_payload", return_value=payload):
            result = self.local.call("get_team_data", {})
        text = result["evidence"]["text"]
        self.assertIn("阵容快照采集时间=未提供", text)
        self.assertIn("出场:未提供", text)
        self.assertIn("进球:0", text)
        self.assertIn("不代表事件发生时间", text)

    def test_refreshing_fixed_team_evidence_cannot_clear_access_restrictions(self):
        payload = {"season": {"label": "2026/27"}, "roster": [{"name": "Local player", "number": "1", "position": "门将"}],
                   "stats": {}, "sources": [], "updated_at": "2026-09-16T00:00:00Z"}
        for settings, suffix in ((dict(self.store.settings(), spoiler_free=True), "safe"),
                                 (dict(self.store.settings(), spoiler_free=False), "full")):
            for restriction in ({"private": True}, {"deleted": True}, {"visibility": "hidden"}, {"visibility": "deleted"}):
                with self.subTest(suffix=suffix, restriction=restriction):
                    eid = "team_data_arsenal_" + suffix
                    self.store.data["evidence"].pop(eid, None)
                    local = LocalTools(self.store, self.run, settings)
                    with patch("server.team_center.team_context_payload", return_value=payload):
                        local.call("get_team_data", {})
                    original = dict(self.store.get("evidence", eid), **restriction)
                    self.store.put("evidence", original)
                    with patch("server.team_center.team_context_payload", return_value=payload) as read_snapshot:
                        with self.assertRaisesRegex(ToolError, "不能通过重新生成"):
                            local.call("get_team_data", {})
                        read_snapshot.assert_not_called()
                    self.assertEqual(self.store.get("evidence", eid), original)
                    passage = local.read_cache[eid]["passages"][0]
                    candidate = submission(facts=[{"title": "原资料", "detail": "原资料不得再次交付。", "evidence_ids": [eid],
                                                  "quotes": [{"evidence_id": eid, "passage_id": passage["id"]}]}])
                    self.assertIsNone(verification.prepare_claims(local, candidate)[1])


class ToolRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_batch_keeps_success_when_one_body_is_missing(self):
        store = FakeStore()
        local = LocalTools(store, store.new_run(), store.settings())
        item = store.get("evidence", "ev_transfer")
        store.put("evidence", dict(item, text="", body_storage="transient"))
        collected = {"evidence_ids": ["ev_injury", "ev_transfer"], "attempted_reads": 2, "errors": [], "coverage": "partial"}
        with patch("server.research.collect_team", new=AsyncMock(return_value=collected)):
            result = await local.acall("research_news", {"query": "training"})
        self.assertEqual(result["evidence_ids"], ["ev_injury"])
        self.assertEqual(result["evidence"][0]["id"], "ev_injury")
        self.assertEqual(result["errors"][0]["evidence_id"], "ev_transfer")
        self.assertEqual(local.read_ids, {"ev_injury"})

    async def test_checker_gets_provider_time_allowance_and_disables_long_reasoning(self):
        store = FakeStore()
        store.config.update(verification_enabled=True, verification_timeout_seconds=45)
        store.config["llm"].update(timeout_seconds=90, thinking_enabled=True)
        store.new_run()
        responses = [tool_message("read_evidence", {"evidence_id": "ev_injury"}),
                     tool_message("submit_answer", quoted_submission()), checker_result()]
        with patch.object(agent, "request_completion", new=AsyncMock(side_effect=responses)) as provider:
            await agent.run_agent(store, "run_one", asyncio.Event())
        self.assertEqual(store.get("runs", "run_one")["verification"]["status"], "supported")
        checker = provider.await_args_list[-1]
        self.assertEqual(checker.kwargs["timeout"], 90)
        self.assertFalse(checker.args[0]["thinking_enabled"])
        self.assertTrue(provider.await_args_list[0].args[0]["thinking_enabled"])

    async def test_source_support_error_keeps_curated_reason(self):
        store = FakeStore()
        local = LocalTools(store, store.new_run(), store.settings())
        local.call("read_evidence", {"evidence_id": "ev_injury"})
        result = await verification.verify_submission(local, quoted_submission(), store.settings()["llm"],
            requester=AsyncMock(side_effect=agent.AgentFailure("请求频率达到限制，HTTP 429。")))
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(any("429" in warning for warning in result["warnings"]))
        self.assertFalse(any(row["verdict"] == "supported" for row in result["claims"]))
