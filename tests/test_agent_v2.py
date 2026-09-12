"""Network scope, immutable skill permissions and resumable evidence state."""
import asyncio
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

import server
from server import agent
from server.agent_tools import LocalTools, ToolError
from tests.test_agent import FakeStore, tool_message


class V2ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeStore()
        self.run = self.store.new_run()
        self.local = LocalTools(self.store, self.run, self.store.settings(), agent.load_skill)
        self.candidate = {"id": "candidate_one", "mode": "live", "team_id": "arsenal", "title": "training injury update", "spoiler": False,
                          "published_at": "2026-08-31T09:00:00Z", "url": "https://example.test/article"}
        self.store.put("candidates", self.candidate)
        self.research = types.ModuleType("server.research")
        self.research.discover_news = AsyncMock(return_value={"candidates": [self.candidate], "errors": []})
        self.research.read_candidate = AsyncMock(return_value={"evidence": self.store.get("evidence", "ev_injury"), "duplicate": True})
        self.research.collect_team = AsyncMock(return_value={"evidence_ids": ["ev_injury", "ev_result", "ev_other_team"], "added": 1, "unchanged": 0, "errors": []})
        self.module_patch = patch.dict(sys.modules, {"server.research": self.research})
        self.attr_patch = patch.object(server, "research", self.research, create=True)
        self.module_patch.start()
        self.attr_patch.start()
        self.addCleanup(self.attr_patch.stop)
        self.addCleanup(self.module_patch.stop)

    async def test_disabled_demo_and_historical_preview_never_make_network_call(self):
        for changes in [{"allow_network": False}, {"mode": "demo"}, {"skill": "match_preview", "match_id": "match_one"}]:
            run = dict(self.run, **changes)
            local = LocalTools(self.store, run, self.store.settings(), agent.load_skill)
            for name, args in [("discover_news", {"query": "injury"}), ("read_article", {"candidate_id": "candidate_one"}),
                               ("refresh_team_sources", {"query": "injury"})]:
                with self.assertRaises(ToolError):
                    await local.acall(name, args)
        self.research.discover_news.assert_not_awaited()
        self.research.read_candidate.assert_not_awaited()
        self.research.collect_team.assert_not_awaited()

    async def test_candidates_are_not_evidence_but_read_article_is(self):
        result = await self.local.acall("discover_news", {"query": "injury"})
        self.assertEqual(len(result["candidates"]), 1)
        self.assertFalse(self.local.read_ids)
        result = await self.local.acall("read_article", {"candidate_id": "candidate_one"})
        self.assertEqual(result["evidence"]["id"], "ev_injury")
        self.assertEqual(self.local.read_ids, {"ev_injury"})

    async def test_cross_team_and_hidden_candidate_blocked_before_fetch(self):
        for changes in [{"team_id": "liverpool"}, {"mode": "demo"}, {"spoiler": True, "title": "Arsenal won 3-1"}]:
            self.store.put("candidates", dict(self.candidate, **changes))
            with self.assertRaises(ToolError):
                await self.local.acall("read_article", {"candidate_id": "candidate_one"})
        self.research.read_candidate.assert_not_awaited()

    async def test_post_fetch_spoiler_is_not_returned_or_marked_read(self):
        self.research.read_candidate.return_value = {"evidence": self.store.get("evidence", "ev_result")}
        with self.assertRaises(ToolError):
            await self.local.acall("read_article", {"candidate_id": "candidate_one"})
        self.assertFalse(self.local.read_ids)

    async def test_review_discovery_reveals_only_selected_match_candidates(self):
        permitted = dict(self.candidate, spoiler=True, title="Selected match report")
        other = dict(permitted, id="other_candidate", title="Other match secret score")
        self.research.discover_news.return_value = {"candidates": [permitted, other], "errors": []}
        self.research.candidate_matches = lambda store, team_id, item, match_id: item["id"] == "candidate_one" and match_id == "match_one"
        local = LocalTools(self.store, dict(self.run, skill="match_review", match_id="match_one"), self.store.settings(), agent.load_skill)
        result = await local.acall("discover_news", {"query": "match report"})
        self.assertEqual([item["id"] for item in result["candidates"]], ["candidate_one"])
        self.assertNotIn("Other match secret", json.dumps(result))

    async def test_collect_filters_ids_and_does_not_grant_citation(self):
        result = await self.local.acall("refresh_team_sources", {"query": "injury", "limit": 2})
        self.assertEqual(result["evidence_ids"], ["ev_injury"])
        self.assertFalse(self.local.read_ids)

    async def test_network_budgets_span_multiple_tool_types(self):
        await self.local.acall("refresh_team_sources", {"query": "injury", "limit": 6})
        await self.local.acall("read_article", {"candidate_id": "candidate_one"})
        await self.local.acall("read_article", {"candidate_id": "candidate_one"})
        with self.assertRaises(ToolError):
            await self.local.acall("read_article", {"candidate_id": "candidate_one"})
        await self.local.acall("discover_news", {"query": "training"})
        await self.local.acall("discover_news", {"query": "fitness"})
        with self.assertRaises(ToolError):
            await self.local.acall("discover_news", {"query": "fourth"})
        self.assertEqual(self.local.budget()["article_reads_used"], 8)

    async def test_skill_composition_does_not_expand_scope_or_network_permission(self):
        local = LocalTools(self.store, dict(self.run, allow_network=False), self.store.settings(), agent.load_skill)
        self.assertTrue(local.call("use_skill", {"skill": "event_watch"})["scope_unchanged"])
        self.assertEqual(local.skills_used, ["ask", "event_watch"])
        self.assertFalse(local.allow_network)
        for name in ["match_review", "match_preview", "../../bad"]:
            with self.assertRaises(ToolError):
                local.call("use_skill", {"skill": name})
        with self.assertRaises(ToolError):
            local.call("read_evidence", {"evidence_id": "ev_result"})

    async def test_network_tool_cancel_propagates(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def slow(*args, **kwargs):
            started.set()
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        self.research.discover_news = slow
        task = asyncio.create_task(self.local.arun_tool_call(tool_message("discover_news", {"query": "injury"})["tool_calls"][0]))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())


class V2HarnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeStore()

    async def test_auto_route_and_skill_composition_are_recorded(self):
        self.store.new_run(skill="auto", question="最近变化有哪些？")
        responses = [tool_message("use_skill", {"skill": "event_watch"}),
                     tool_message("read_evidence", {"evidence_id": "ev_injury"}),
                     {"content": "来源提到训练缺席。[ev_injury]"}]
        with patch.object(agent, "request_completion", new=AsyncMock(side_effect=responses)):
            await agent.run_agent(self.store, "run_one", asyncio.Event())
        result = self.store.get("runs", "run_one")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["skill"], "team_brief")
        self.assertEqual(result["skills_used"], ["team_brief", "event_watch"])
        self.assertEqual(result["protocol"], "chat_completions")
        self.assertEqual(result["read_evidence_ids"], ["ev_injury"])

    async def test_resume_rereads_scoped_evidence_instead_of_private_context(self):
        self.store.new_run(id="parent", status="failed", read_evidence_ids=["ev_injury", "ev_other_team", "ev_result"],
                           answer="", conversation_id="old")
        self.store.new_run(parent_run_id="parent")
        captured = []
        async def answer(config, messages, **kwargs):
            captured.extend(messages)
            return {"content": "继续查得来源提到训练缺席。[ev_injury]"}
        with patch.object(agent, "request_completion", new=answer):
            await agent.run_agent(self.store, "run_one", asyncio.Event())
        result = self.store.get("runs", "run_one")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["read_evidence_ids"], ["ev_injury"])
        self.assertIn("Medical update", json.dumps(captured))
        self.assertNotIn("The striker", json.dumps(captured))
        self.assertNotIn("3-1", json.dumps(captured))

    async def test_resume_cross_team_or_changed_match_scope_rejected(self):
        for patches in [{"team_id": "liverpool"}, {"match_id": "different"}]:
            self.store.new_run(id="parent", status="failed", **patches)
            self.store.new_run(parent_run_id="parent")
            with patch.object(agent, "request_completion", new=AsyncMock()) as mocked:
                await agent.run_agent(self.store, "run_one", asyncio.Event())
            self.assertEqual(self.store.get("runs", "run_one")["status"], "failed")
            mocked.assert_not_awaited()

    async def test_tool_result_continuation_is_required_to_pass_diagnostics(self):
        sequence = [{"content": "OK"}, tool_message("connection_probe", {"nonce": "touchline_connection_check"}), {"content": "I support tools"}]
        with patch.object(agent, "request_completion", new=AsyncMock(side_effect=sequence)):
            result = await agent.test_connection(self.store.config["llm"])
        self.assertFalse(result["ok"])
        self.assertFalse(result["tool_support"])
        self.assertEqual([item["ok"] for item in result["checks"]], [True, True, False])

    async def test_safe_news_can_be_read_after_model_prematurely_stops(self):
        article = {"id": "ev_travel", "mode": "live", "team_id": "arsenal", "origin": "web", "spoiler": True,
                   "title": "Arsenal flight delayed ahead of European fixture", "url": "https://example.test/flight-delayed",
                   "published_at": "2026-09-08T12:00:00Z", "fetched_at": "2026-09-09T01:00:00Z", "source_name": "Test source",
                   "text": "The Arsenal flight was delayed because of air traffic disruption, and the manager cancelled the scheduled press conference before the next fixture.\nArsenal won the previous match 3-1 with a late goal.\nClub sources say that the training programme has not changed and the squad will arrive at the hotel later than planned for the upcoming European fixture."}
        self.store.put("evidence", article)
        self.store.new_run(question="Arsenal flight delayed", allow_network=False)
        messages = []
        sequence = [{"content": "资料不足。"}, tool_message("read_evidence", {"evidence_id": "ev_travel"}),
                    {"content": "来源称航班延误，发布会取消；其他训练安排未变。[ev_travel]"}]
        async def answer(config, context, **kwargs):
            messages.extend(context)
            return sequence.pop(0)
        with patch.object(agent, "request_completion", new=answer):
            await agent.run_agent(self.store, "run_one", asyncio.Event())
        run = self.store.get("runs", "run_one")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["redacted_evidence_ids"], ["ev_travel"])
        self.assertIn("flight was delayed", json.dumps(messages))
        self.assertNotIn("3-1", json.dumps(messages))
        self.assertTrue(self.store.get("evidence", "ev_travel")["spoiler"])
        logged = [step for step in run["steps"] if step.get("tool") == "read_evidence"]
        self.assertEqual(logged[0]["arguments"], {"evidence_id": "ev_travel"})
        self.assertNotIn("secret-example", json.dumps(run))
