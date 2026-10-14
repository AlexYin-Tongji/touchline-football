"""Workflow contracts and controlled efficiency checks, not model quality scores."""
import asyncio
import copy
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from server import agent
from server.agent_tools import LocalTools, ToolError
from tests.test_agent import FakeStore, tool_message
from tests.test_news_reports import manuscript


class SkillAndDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def test_primary_skills_and_legacy_workflows_remain_loadable(self):
        # team-maintenance is the maintenance entrypoint. team is the panel
        # refresh, registered beside it rather than inside the question skills.
        self.assertEqual({s["id"] for s in agent.CAPABILITIES["skills"]},
                         {"team-maintenance", "team", "team-news", "team-information", "football-assistant"})
        for mode in agent.SKILLS:
            self.assertTrue(agent.load_skill(mode).strip())
        for name in ("team-maintenance", "team", "team-news", "team-information", "football-assistant"):
            self.assertTrue(agent.load_skill(name).strip())
        self.assertIn("球队中心", agent.load_skill("team"))

    def test_skill_workflow_selection_keeps_match_and_network_boundaries(self):
        store = FakeStore()
        local = LocalTools(store, store.new_run(allow_network=False), store.settings(), agent.load_skill)
        result = local.call("use_skill", {"skill": "football-assistant", "workflow": "transfer_check"})
        self.assertTrue(result["scope_unchanged"])
        self.assertFalse(local.allow_network)
        repeated = local.call("use_skill", {"skill": "transfer_check"})
        self.assertTrue(repeated.get("already_loaded"))
        self.assertFalse(repeated.get("instructions"))
        with self.assertRaises(ToolError):
            local.call("use_skill", {"skill": "football-assistant", "workflow": "match_review"})
        with self.assertRaises(ToolError):
            local.call("use_skill", {"skill": "team-information", "workflow": "transfer_check"})

    def test_complete_news_does_not_require_filler_analysis_or_unknowns(self):
        store = FakeStore()
        local = LocalTools(store, store.new_run(skill="news_report"), store.settings())
        local.call("read_evidence", {"evidence_id": "ev_injury"})
        value = manuscript()
        value.update(analysis="", unknowns=[])
        result = local.call("submit_answer", value)
        self.assertTrue(result["submitted"])
        delivered = local.submission["structured_answer"]["facts"]
        self.assertEqual([(f["title"], f["detail"], f["evidence_ids"]) for f in delivered],
                         [(f["title"], f["detail"], f["evidence_ids"]) for f in value["facts"]])
        self.assertTrue(all(f["quotes"][0].get("quote") for f in delivered))
        self.assertEqual(local.submission["structured_answer"]["analysis"], "")
        self.assertEqual(local.submission["structured_answer"]["unknowns"], [])

    async def test_equivalent_unicode_wire_encoding_keeps_same_valid_delivery(self):
        store = FakeStore()
        value = manuscript()
        value["facts"] = value["facts"][:1]
        value["facts"][0]["detail"] = "中场球员因伤暂时无法参加训练，医疗更新说明了当前状态。" * 400
        results = []
        for ascii_only in (False, True):
            local = LocalTools(store, store.new_run(skill="news_report"), store.settings())
            local.call("read_evidence", {"evidence_id": "ev_injury"})
            call = tool_message("submit_answer", value)["tool_calls"][0]
            call["function"]["arguments"] = json.dumps(value, ensure_ascii=ascii_only)
            result = await local.arun_tool_call(call)
            self.assertTrue(result.get("submitted"), result)
            results.append(local.submission["structured_answer"])
        self.assertEqual(results[0], results[1])

    async def test_selected_sources_overlap_and_keep_input_order(self):
        store = FakeStore()
        ids = ["ev_injury", "ev_transfer", "ev_third"]
        store.put("evidence", {**store.get("evidence", "ev_injury"), "id": ids[2]})
        store.new_run(input_evidence_ids=ids)
        active = 0
        peak = 0
        original = LocalTools.acall
        async def delayed(local, name, args):
            nonlocal active, peak
            if name != "read_evidence":
                return await original(local, name, args)
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep({ids[0]: .09, ids[1]: .05, ids[2]: .025}[args["evidence_id"]])
                return await original(local, name, args)
            finally:
                active -= 1
        observed = []
        async def model(config, messages, **kwargs):
            for message in messages:
                if message.get("role") == "user" and "本轮实际读取的原文" in message.get("content", ""):
                    observed.append(json.loads(message["content"].split("\n", 1)[1])["id"])
            return {"content": "医疗更新提到中场球员暂时无法参加训练。 [ev_injury]"}
        with patch.object(LocalTools, "acall", new=delayed), patch.object(agent, "request_completion", new=model):
            started = time.perf_counter()
            await agent.run_agent(store, "run_one", asyncio.Event())
            elapsed = time.perf_counter() - started
        self.assertEqual(store.get("runs", "run_one")["status"], "completed")
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 3)
        self.assertEqual(observed, ids)
        metrics = store.get("runs", "run_one").get("metrics", {})
        self.assertEqual(metrics.get("selected_sources_read"), 3)
        self.assertEqual(metrics.get("draft_model_calls"), 1)
        self.assertGreater(metrics.get("source_read_ms", 0), 30)
        print(f"selected_sources_mock_seconds={elapsed:.3f}; peak={peak}")

    async def test_parallel_read_rechecks_fast_source_before_sending_to_model(self):
        store = FakeStore()
        store.new_run(input_evidence_ids=["ev_injury", "ev_transfer"])
        read = LocalTools.acall
        fast_read = asyncio.Event()
        async def delayed(local, name, args):
            if name == "read_evidence" and args["evidence_id"] == "ev_injury":
                await asyncio.sleep(.05)
            result = await read(local, name, args)
            if name == "read_evidence" and args["evidence_id"] == "ev_transfer":
                fast_read.set()
            return result
        async def hide():
            await fast_read.wait()
            source = store.get("evidence", "ev_transfer")
            store.put("evidence", {**source, "private": True})
        model = AsyncMock(return_value={"content": "材料不足。"})
        with patch.object(LocalTools, "acall", new=delayed), patch.object(agent, "request_completion", new=model):
            await asyncio.gather(agent.run_agent(store, "run_one", asyncio.Event()), hide())
        model.assert_not_awaited()
        self.assertEqual(store.get("runs", "run_one")["status"], "failed")

    async def test_queued_model_rechecks_source_before_actual_provider_call(self):
        from server.model_harness import ModelHarness
        store = FakeStore()
        store.new_run(input_evidence_ids=["ev_injury"])
        harness = store._model_harness = ModelHarness(global_limit=1, provider_limit=1)
        entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def occupy():
            entered.set()
            await release.wait()
        holder = asyncio.create_task(harness.request(store.settings()["llm"], occupy, owner_id="busy", role="chat", stage="answer"))
        await entered.wait()
        real_request = harness.request
        async def watched(*args, **kwargs):
            if kwargs.get("owner_id") == "run_one":
                queued.set()
            return await real_request(*args, **kwargs)
        model = AsyncMock(return_value={"content": "材料不足。"})
        with patch.object(harness, "request", new=watched), patch.object(agent, "request_completion", new=model):
            task = asyncio.create_task(agent.run_agent(store, "run_one", asyncio.Event()))
            try:
                await asyncio.wait_for(queued.wait(), 1)
                source = store.get("evidence", "ev_injury")
                store.put("evidence", {**source, "private": True})
            finally:
                release.set()
                await holder
                await task
        model.assert_not_awaited()
        self.assertEqual(store.get("runs", "run_one")["status"], "failed")
