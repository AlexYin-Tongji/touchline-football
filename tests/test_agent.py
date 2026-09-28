"""Deterministic scope, protocol and lifecycle tests; no paid model required."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from server import agent
from server.agent_tools import LocalTools, ToolError


class FakeStore:
    def __init__(self):
        self.data = {name: {} for name in ("teams", "events", "evidence", "matches", "runs")}
        self.config = {"spoiler_free": True, "explanation_depth": "standard", "llm": {"base_url": "http://localhost:9999/v1", "model": "test", "api_key": "secret-example", "max_rounds": 5, "timeout_seconds": 10}}
        self.put("teams", {"id": "arsenal", "name": "阿森纳", "english_name": "Arsenal"})
        for eid, mode, team, text, spoiler in [
            ("ev_injury", "live", "arsenal", "Medical update: the midfielder has an injury and is unavailable for training.", False),
            ("ev_transfer", "live", "arsenal", "The club reports a contract signing and a transfer discussion.", False),
            ("ev_result", "live", "arsenal", "The match ended 3-1; the selected match report is available.", True),
            ("ev_other_result", "live", "arsenal", "Another match ended 0-7.", True),
            ("ev_demo", "demo", "arsenal", "虚构示例：中场球员伤病恢复，训练强度仍待确认。", False),
            ("ev_other_team", "live", "liverpool", "The striker's injury is confirmed.", False),
        ]:
            self.put("evidence", {"id": eid, "mode": mode, "team_id": team, "title": text[:50], "text": text,
                                  "spoiler": spoiler, "published_at": "2026-08-31T09:00:00Z", "fetched_at": "2026-08-31T10:00:00Z", "source_name": "测试资料", "origin": "manual", "url": ""})
        self.put("matches", {"id": "match_one", "mode": "live", "team_id": "arsenal", "opponent": "Test City", "kickoff": "2026-09-01T10:00:00Z", "status": "finished", "score": "3-1", "note": "A 3-1 win", "evidence_ids": ["ev_injury", "ev_result"]})

    def put(self, collection, item):
        self.data.setdefault(collection, {})[item["id"]] = copy.deepcopy(item)
        return copy.deepcopy(item)

    def get(self, collection, item_id):
        return copy.deepcopy(self.data.get(collection, {}).get(item_id))

    def list(self, collection, mode=None, team_id=None):
        return [copy.deepcopy(item) for item in self.data.get(collection, {}).values()
                if (mode is None or item.get("mode") == mode) and (team_id is None or item.get("team_id") == team_id)]

    def settings(self):
        return copy.deepcopy(self.config)

    def new_run(self, **patches):
        result = {"id": "run_one", "mode": "live", "team_id": "arsenal", "skill": "ask", "question": "伤病消息确认了吗？", "conversation_id": "conversation_one", "status": "queued", "answer": "", "evidence_ids": [], "warnings": [], "steps": [], "error": None, "created_at": "2026-09-09T10:00:00Z", "finished_at": None, "model": ""}
        result.update(patches)
        return self.put("runs", result)


def tool_message(name, args, call_id="call_1"):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class LocalToolTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.run = self.store.new_run()
        self.local = LocalTools(self.store, self.run, self.store.settings())

    def test_mode_team_and_spoiler_scope(self):
        for eid in ("ev_demo", "ev_other_team", "ev_result", "missing"):
            with self.assertRaises(ToolError):
                self.local.call("read_evidence", {"evidence_id": eid})
        self.assertEqual(self.local.read_ids, set())
        self.local.call("read_evidence", {"evidence_id": "ev_injury"})
        self.assertEqual(self.local.read_ids, {"ev_injury"})

    def test_bilingual_search_and_no_unrelated_fallback(self):
        for query in ("最新伤病怎么样", "midfielder injury update"):
            result = self.local.call("search_evidence", {"query": query})
            self.assertEqual(result["results"][0]["id"], "ev_injury")
        self.assertEqual(self.local.call("search_evidence", {"query": "宇宙飞船量子芯片"})["results"], [])
        self.assertFalse(self.local.read_ids, "search snippets are not read evidence")

    def test_tool_schema_rejects_extra_keys_types_and_code(self):
        for name, args in [("exec", {"cmd": "anything"}), ("team_context", {"url": "http://localhost"}),
                           ("search_evidence", {"query": "injury", "limit": True}), ("read_evidence", {"evidence_id": 5})]:
            with self.assertRaises(ToolError):
                self.local.call(name, args)

    def test_context_hides_finished_score_and_note(self):
        result = self.local.call("team_context", {})
        text = json.dumps(result)
        self.assertNotIn("3-1", text)
        self.assertNotIn("ev_result", text)

    def test_context_includes_compact_decision_history_without_granting_citations(self):
        import tempfile
        from pathlib import Path
        from server.storage import Store

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "isolated.sqlite3")
            try:
                store.put("teams", self.store.get("teams", "arsenal"))
                store.put("runs", self.run)
                store.put("decisions", {
                    "id": "decision_one", "mode": "live", "team_id": "arsenal",
                    "created_at": "2026-09-28T08:00:00Z", "status": "completed",
                    "reason": "阵容资料超期", "summary": "来源读取完成，已记录前后变化。",
                    "parts": ["roster", "schedule"],
                    "before": {"roster": {"values": [{"name": "PRIVATE_DETAIL"}]},
                               "schedule": {"values": [{"score": "9-0"}]}},
                    "changes": [{"field": "roster", "changed": True, "confirmed": True,
                                 "last_confirmed_at": "2026-09-28T08:00:00Z"},
                                {"field": "schedule", "changed": True, "confirmed": True,
                                 "last_confirmed_at": "2026-09-28T08:00:00Z"}],
                })
                local = LocalTools(store, self.run, store.settings())
                result = local.call("context_pack", {})
                history = result["decision_history"]
                self.assertEqual(history["items"][0]["reason"], "阵容资料超期")
                self.assertEqual(history["items"][0]["changes"][0]["last_confirmed_at"], "2026-09-28T08:00:00Z")
                self.assertNotIn("PRIVATE_DETAIL", json.dumps(history))
                self.assertNotIn("9-0", json.dumps(history))
                self.assertNotIn("changed", history["items"][0]["changes"][1])
                self.assertFalse(local.read_ids)
                # Current maintenance must not leak observations from after a
                # historical preview cutoff or outside a fixed article.
                store.put("matches", self.store.get("matches", "match_one"))
                for scope in ({"skill": "match_preview", "match_id": "match_one"},
                              {"skill": "news_report", "report_id": "fixed_article"}):
                    scoped = LocalTools(store, {**self.run, **scope}, store.settings())
                    self.assertEqual(scoped.call("context_pack", {})["decision_history"]["items"], [])
            finally:
                store.close()

    def test_preview_uses_strict_availability_cutoff(self):
        self.run.update(skill="match_preview", match_id="match_one")
        local = LocalTools(self.store, self.run, self.store.settings())
        self.assertTrue(local.evidence_allowed(self.store.get("evidence", "ev_injury")))
        value = self.store.get("evidence", "ev_injury")
        value["published_at"] = None
        self.assertTrue(local.evidence_allowed(value), "unknown publication may use known pre-match capture")
        value["fetched_at"] = "2026-09-01T10:00:00Z"
        self.assertFalse(local.evidence_allowed(value))
        value["published_at"] = "2026-08-31T09:00:00Z"
        self.assertFalse(local.evidence_allowed(value), "later capture can contain a later article edit")
        self.assertIsNone(local.call("match_context", {"match_id": "match_one"})["score"])

    def test_review_reveals_selected_match_only(self):
        self.run.update(skill="match_review", match_id="match_one")
        local = LocalTools(self.store, self.run, self.store.settings())
        self.assertEqual(local.call("read_evidence", {"evidence_id": "ev_result"})["id"], "ev_result")
        with self.assertRaises(ToolError):
            local.call("read_evidence", {"evidence_id": "ev_other_result"})
        with self.assertRaises(ToolError):
            local.call("read_evidence", {"evidence_id": "ev_transfer"})
        self.assertEqual(local.call("search_evidence", {"query": "transfer"})["results"], [],
                         "even non-spoiler same-team articles require a link to this match")
        self.assertEqual(local.call("match_context", {"match_id": "match_one"})["score"], "3-1")

    def test_review_unfinished_and_cross_scope_match_rejected(self):
        match = self.store.get("matches", "match_one")
        match["status"] = "scheduled"
        self.store.put("matches", match)
        self.run.update(skill="match_review", match_id="match_one")
        with self.assertRaises(ToolError):
            LocalTools(self.store, self.run, self.store.settings())
        self.run.update(skill="match_preview", mode="demo")
        with self.assertRaises(ToolError):
            LocalTools(self.store, self.run, self.store.settings())

    def test_match_context_prior_observations_preserve_all_scoped_history(self):
        for mode in ("live", "demo"):
            with self.subTest(mode=mode):
                store = FakeStore()
                match = store.get("matches", "match_one")
                match["mode"] = mode
                store.put("matches", match)
                run = store.new_run(mode=mode, skill="match_review", match_id="match_one")
                base = dict(run, skill="match_preview", status="completed", conversation_id="old_conversation",
                            evidence_ids=[], answer="当时的待验证假设" * 500)
                for index in range(3):
                    store.put("runs", dict(base, id=f"prior_{index}", created_at=f"2026-08-31T0{index}:00:00Z"))
                for key, patch_values in [
                    ("foreign_team", {"team_id": "liverpool"}),
                    ("foreign_mode", {"mode": "demo" if mode == "live" else "live"}),
                    ("foreign_match", {"match_id": "another_match"}),
                    ("not_completed", {"status": "running"}),
                    ("not_preview", {"skill": "ask"}),
                    ("at_kickoff", {"created_at": match["kickoff"]}),
                    ("after_kickoff", {"created_at": "2026-09-02T01:00:00Z"}),
                    ("unknown_time", {"created_at": "unknown"}),
                ]:
                    row = dict(base, id=key, created_at="2026-08-31T09:00:00Z")
                    row.update(patch_values)
                    store.put("runs", row)
                local = LocalTools(store, run, store.settings())
                result = local.call("match_context", {"match_id": "match_one"})["prior_observations"]
                self.assertEqual([item["run_id"] for item in result], ["prior_2", "prior_1", "prior_0"])
                self.assertTrue(all(item["answer"] == base["answer"] and not item["truncated"] for item in result))
                self.assertTrue(all("不是事实" in item["notice"] and "read_evidence" in item["notice"] for item in result))
                self.assertFalse(local.read_ids, "historical observations do not grant evidence citations")

    def test_event_timeline_drops_foreign_and_hidden_evidence(self):
        self.store.put("events", {"id": "event_one", "team_id": "arsenal", "mode": "live", "title": "状态变化", "evidence_ids": ["ev_injury", "ev_other_team"], "timeline": [{"at": "2026-08-31", "text": "visible", "evidence_id": "ev_injury"}, {"at": "2026-09-02", "text": "other team secret", "evidence_id": "ev_other_team"}]})
        result = self.local.call("event_timeline", {"event_id": "event_one"})
        self.assertEqual(len(result["timeline"]), 1)
        self.assertEqual(result["evidence_ids"], ["ev_injury"])

    def test_history_rechecks_mode_team_spoilers_and_conversation(self):
        for key, patches in [("valid", {}), ("foreign", {"team_id": "liverpool"}), ("demo", {"mode": "demo"}),
                             ("hidden", {"spoiler": True}), ("otherchat", {"conversation_id": "different"})]:
            row = dict(self.run, id=key, status="completed", answer=key, evidence_ids=["ev_injury"])
            row.update(patches)
            self.store.put("runs", row)
        result = agent.history_messages(self.store, self.run, self.local)
        self.assertEqual(len(result), 2)
        self.assertIn("valid", result[-1]["content"])

    def test_all_skills_are_real_runtime_files(self):
        for skill in agent.SKILLS:
            instructions = agent.load_skill(skill)
            self.assertTrue(instructions.strip())
            self.assertFalse(instructions.startswith("---"), "discovery metadata is not execution instructions")
        with self.assertRaises(agent.AgentFailure):
            agent.load_skill("../../other")


class AgentLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeStore()
        self.store.new_run()
        self.cancel = asyncio.Event()

    async def run_with(self, responses):
        with patch.object(agent, "request_completion", new=AsyncMock(side_effect=responses)) as provider:
            await agent.run_agent(self.store, "run_one", self.cancel)
        return self.store.get("runs", "run_one"), provider

    async def test_live_read_then_cited_answer(self):
        result, provider = await self.run_with([tool_message("search_evidence", {"query": "injury"}),
                                               tool_message("read_evidence", {"evidence_id": "ev_injury"}),
                                               {"content": "来源称中场仍受伤病影响，是否已恢复未知。[ev_injury]"}])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["evidence_ids"], ["ev_injury"])
        self.assertEqual(provider.await_count, 3)
        self.assertNotIn("secret-example", json.dumps(result))

    async def test_unprotected_run_remains_hidden_after_settings_change(self):
        self.store.config["spoiler_free"] = False
        result, _ = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
                                         {"content": "来源提到伤病。[ev_injury]"}])
        self.assertTrue(result["spoiler"], "even a non-review task may have seen result context")

    async def test_missing_llm_never_uses_demo(self):
        self.store.config["llm"]["base_url"] = ""
        result, provider = await self.run_with([])
        self.assertEqual(result["status"], "failed")
        self.assertIn("配置模型", result["error"])
        provider.assert_not_awaited()

    async def test_demo_uses_local_tools_and_explicit_label(self):
        self.store.new_run(mode="demo")
        result, provider = await self.run_with([])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["answer"].count("虚构示例，非真实新闻"), 1)
        self.assertNotIn("未调用模型", result["answer"])
        self.assertIn("中场球员伤病恢复", result["answer"])
        self.assertIn("ev_demo", result["evidence_ids"])
        self.assertNotIn("ev_injury", result["evidence_ids"])
        provider.assert_not_awaited()

    async def test_unread_or_foreign_citation_repaired_once_then_rejected(self):
        result, provider = await self.run_with([{"content": "不应展示的跨球队说法 [ev_other_team]"}, {"content": "依旧错误 [ev_other_team]"}])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(provider.await_count, 2)
        self.assertEqual(result["answer"], "")
        self.assertEqual(result["evidence_ids"], [])

    async def test_missing_citation_can_be_repaired(self):
        result, provider = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
                                               {"content": "来源提到伤病。"}, {"content": "来源提到伤病。[ev_injury]"}])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(provider.await_count, 3)

    async def test_no_read_evidence_suppresses_model_knowledge(self):
        result, _ = await self.run_with([{"content": "凭记忆虚构实时比分为9比8。"}, {"content": "仍未读取资料。"}])
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("9比8", result["answer"])
        self.assertIn("资料不足", result["answer"])

    async def test_repeated_tool_loop_without_progress_stops(self):
        self.store.config["llm"]["max_rounds"] = 2
        result, provider = await self.run_with([tool_message("team_context", {})] * agent.MAX_REPEATED_TOOL_REQUESTS)
        self.assertEqual(result["status"], "failed")
        self.assertIn("没有新进展", result["error"])
        self.assertEqual(provider.await_count, agent.MAX_REPEATED_TOOL_REQUESTS)

    async def test_progressing_research_can_continue_past_old_round_setting(self):
        self.store.config["llm"]["max_rounds"] = 2
        responses = [tool_message("search_evidence", {"query": "injury " + str(index)}) for index in range(18)]
        responses.extend([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
                          {"content": "来源提到这名中场目前有伤情，需要继续关注。[ev_injury]"}])
        result, provider = await self.run_with(responses)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(provider.await_count, 20)
        self.assertEqual(result["progress"]["round"], 20)

    async def test_cancel_interrupts_inflight_request(self):
        started = asyncio.Event()
        was_cancelled = asyncio.Event()
        async def waiting(*args, **kwargs):
            started.set()
            try:
                await asyncio.sleep(50)
            except asyncio.CancelledError:
                was_cancelled.set()
                raise
        with patch.object(agent, "request_completion", new=waiting):
            task = asyncio.create_task(agent.run_agent(self.store, "run_one", self.cancel))
            await started.wait()
            self.cancel.set()
            await asyncio.wait_for(task, timeout=1)
        self.assertTrue(was_cancelled.is_set())
        self.assertEqual(self.store.get("runs", "run_one")["status"], "cancelled")

    async def test_error_never_echoes_provider_secret_url(self):
        result, _ = await self.run_with([RuntimeError("http://provider/path?key=secret-example")])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("secret-example", json.dumps(result))
        self.assertNotIn("provider/path", json.dumps(result))

    async def test_task_deadline_cancels_underlying_coroutine(self):
        with self.assertRaises(agent.AgentFailure):
            await agent.cancellable(asyncio.sleep(5), self.cancel, __import__("time").monotonic() + 0.01)

    async def test_connection_requires_exact_tool_and_argument(self):
        for value, supported in [
            (tool_message("connection_probe", {"nonce": "touchline_connection_check"}), True),
            ({"content": "I support tools"}, False),
            (tool_message("connection_probe", {"nonce": "wrong"}), False),
            (tool_message("different_tool", {"nonce": "touchline_connection_check"}), False),
        ]:
            async def checked_response(config, messages, **kwargs):
                if messages[-1]["role"] == "tool":
                    return {"content": json.loads(messages[-1]["content"])["result"]}
                return value if kwargs.get("tools") else {"content": "OK"}
            with patch.object(agent, "request_completion", new=checked_response):
                result = await agent.test_connection(self.store.config["llm"])
                self.assertEqual(result["ok"], supported)
                self.assertEqual(result["tool_support"], supported)
                self.assertEqual([item["stage"] for item in result["checks"]], ["connection", "generation", "tools"])

    async def test_connection_rejects_key_in_url_without_echo(self):
        config = dict(self.store.config["llm"], base_url="http://localhost/v1?key=secret-example")
        result = await agent.test_connection(config)
        self.assertFalse(result["ok"])
        self.assertNotIn("secret-example", json.dumps(result))

    async def test_no_key_local_endpoint_is_valid_configuration(self):
        config = dict(self.store.config["llm"], api_key="")
        async def checked_response(config, messages, **kwargs):
            if messages[-1]["role"] == "tool":
                return {"content": json.loads(messages[-1]["content"])["result"]}
            return tool_message("connection_probe", {"nonce": "touchline_connection_check"}) if kwargs.get("tools") else {"content": "OK"}
        with patch.object(agent, "request_completion", new=checked_response):
            result = await agent.test_connection(config)
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
