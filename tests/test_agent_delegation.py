"""Real task trees with synthetic models; no provider/network or personal data."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import agent
from server.agent_delegation import Delegation, runtime_for, source_version
from server.agent_tools import LocalTools, ToolError
from server.model_harness import ModelHarness
from tests.test_agent import FakeStore, tool_message
from tests.test_verification import checker_result, quoted_submission


def submission(eid="ev_injury"):
    value = quoted_submission()
    value["facts"][0]["evidence_ids"] = [eid]
    value["facts"][0]["quotes"][0]["evidence_id"] = eid
    return value


class DelegationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_provider_call_ids_keep_bounded_runtime_idempotency(self):
        import time
        store = FakeStore()
        run = store.new_run()
        local = LocalTools(store, run, store.settings())
        local.delegation = Delegation(store, run, local, store.settings(), asyncio.Event(), time.monotonic() + 30)
        identity = "provider-signature-" + "x" * 500
        call = tool_message("spawn_agent", {"task": "读取测试资料"}, identity)["tool_calls"][0]
        try:
            first = await local.arun_tool_call(call)
            self.assertNotIn("error", {k: v for k, v in first.items() if v})
            again = await local.arun_tool_call(call)
            self.assertEqual(first["id"], again["id"])
            distinct = await local.arun_tool_call({**call, "id": identity + "y"})
            self.assertNotEqual(first["id"], distinct["id"])
            changed = tool_message("spawn_agent", {"task": "不同任务"}, identity)["tool_calls"][0]
            self.assertIn("error", await local.arun_tool_call(changed))
            self.assertEqual(call["id"], identity, "provider protocol IDs must remain unchanged")
        finally:
            await runtime_for(store).shutdown()

    async def test_release_of_completed_child_is_recorded_as_success(self):
        store = FakeStore()
        store.new_run()
        child_id = None
        parent_round = 0
        released = False
        parent_read = False
        async def provider(config, messages, **options):
            nonlocal child_id, parent_round, released, parent_read
            if config["_session_id"].startswith("agent_"):
                if not any(m.get("role") == "tool" for m in messages):
                    return tool_message("read_evidence", {"evidence_id": "ev_injury"})
                return tool_message("submit_answer", submission())
            parent_round += 1
            if parent_round == 1:
                return tool_message("spawn_agent", {"task": "查阅训练状态"})
            if parent_round == 2:
                child_id = json.loads(messages[-1]["content"])["id"]
                return tool_message("wait_agents", {"agent_ids": [child_id], "timeout_seconds": 2})
            if not released:
                latest = json.loads(messages[-1]["content"])
                if latest.get("agents") and latest["agents"][0]["status"] != "completed":
                    return tool_message("wait_agents", {"agent_ids": [child_id], "after_cursor": latest["cursor"], "timeout_seconds": 2})
                released = True
                return tool_message("release_agent", {"agent_id": child_id})
            if not parent_read:
                parent_read = True
                return tool_message("read_evidence", {"evidence_id": "ev_injury"})
            return tool_message("submit_answer", submission())
        with patch.object(agent, "request_completion", new=provider):
            await asyncio.wait_for(agent.run_agent(store, "run_one", asyncio.Event()), 3)
        run = store.get("runs", "run_one")
        self.assertEqual(run["status"], "completed")
        self.assertEqual([s["status"] for s in run["steps"] if s.get("tool") in {"spawn_agent", "release_agent"}],
                         ["completed", "completed"])

    async def test_parent_spawns_overlapping_children_then_reads_their_sources(self):
        store = FakeStore()
        store.new_run()
        parent_round = 0
        child_rounds = {}
        child_started = set()
        both_started = asyncio.Event()
        handles = []
        async def provider(config, messages, **options):
            nonlocal parent_round
            session = config.get("_session_id", "")
            if session.startswith("agent_"):
                child_rounds[session] = child_rounds.get(session, 0) + 1
                child_started.add(session)
                if len(child_started) == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), 1)
                # Children receive only their explicit work, not this private history.
                self.assertNotIn("PRIVATE_PARENT_HISTORY", json.dumps(messages))
                if child_rounds[session] == 1:
                    return tool_message("read_evidence", {"evidence_id": "ev_injury"})
                return tool_message("submit_answer", submission())
            parent_round += 1
            names = {t["function"]["name"] for t in options["tools"]}
            self.assertIn("spawn_agent", names)
            if parent_round == 1:
                return {"role": "assistant", "content": None, "tool_calls": [
                    tool_message("spawn_agent", {"task": "独立查阅训练状态", "context": "只返回资料依据"}, f"spawn_{i}")["tool_calls"][0]
                    for i in range(2)]}
            if parent_round == 2:
                handles.extend(json.loads(m["content"])["id"] for m in messages if m.get("role") == "tool")
                self.assertEqual(len(handles), 2)
                return tool_message("wait_agents", {"agent_ids": handles, "timeout_seconds": 5})
            if parent_round == 3:
                result = json.loads(messages[-1]["content"])
                # wait returns on first completion; the second remains independent.
                self.assertTrue(any(a["status"] == "completed" for a in result["agents"]))
                return tool_message("submit_answer", submission())
            if parent_round == 4:
                self.assertIn("error", json.loads(messages[-1]["content"]))
                self.assertFalse(store.get("runs", "run_one")["answer"])
                return tool_message("read_evidence", {"evidence_id": "ev_injury"})
            return tool_message("submit_answer", submission())
        store.new_run(id="past", status="completed", answer="PRIVATE_PARENT_HISTORY", evidence_ids=[])
        store.new_run()
        with patch.object(agent, "request_completion", new=provider):
            await asyncio.wait_for(agent.run_agent(store, "run_one", asyncio.Event()), 3)
        self.assertEqual(store.get("runs", "run_one")["status"], "completed")
        spawn_steps = [step for step in store.get("runs", "run_one")["steps"] if step.get("tool") == "spawn_agent"]
        self.assertEqual([step["status"] for step in spawn_steps], ["completed", "completed"])
        self.assertTrue(all(step["detail"] for step in spawn_steps))
        self.assertEqual(len(child_started), 2)
        self.assertEqual({r["id"] for r in store.list("runs")}, {"past", "run_one"})
        self.assertTrue(store.list("agent_tasks"))
        self.assertNotIn("Medical update", json.dumps(store.list("agent_tasks")))

    async def test_verification_rejects_source_changed_while_checker_waited(self):
        store = FakeStore()
        store.config["verification_enabled"] = True
        store.new_run()
        replies = [tool_message("read_evidence", {"evidence_id": "ev_injury"}),
                   tool_message("submit_answer", submission())]
        async def provider(config, messages, **options):
            if replies:
                return replies.pop(0)
            source = store.get("evidence", "ev_injury")
            store.put("evidence", {**source, "text": "Correction: the midfielder is healthy and training."})
            return checker_result()
        with patch.object(agent, "request_completion", new=provider):
            await agent.run_agent(store, "run_one", asyncio.Event())
        run = store.get("runs", "run_one")
        self.assertEqual(run["status"], "failed")
        self.assertFalse(run.get("answer"))
        self.assertIn("变化", run["error"])

    async def test_single_model_slot_parent_wait_does_not_deadlock(self):
        store = FakeStore()
        store._model_harness = ModelHarness(global_limit=1, provider_limit=1)
        store.new_run()
        rounds = {}
        async def provider(config, messages, **options):
            session = config["_session_id"]
            rounds[session] = rounds.get(session, 0) + 1
            n = rounds[session]
            if session.startswith("agent_"):
                return tool_message("read_evidence", {"evidence_id": "ev_injury"}) if n == 1 else tool_message("submit_answer", submission())
            if n == 1:
                return tool_message("spawn_agent", {"task": "查阅训练原文"})
            if n == 2:
                return tool_message("wait_agents", {"timeout_seconds": 5})
            if n == 3:
                result = json.loads(messages[-1]["content"])
                if not any(row["status"] == "completed" for row in result["agents"]):
                    return tool_message("wait_agents", {"after_cursor": result["cursor"], "timeout_seconds": 5})
            if not any(m.get("role") == "tool" and '"ev_injury"' in m.get("content", "") and '"passages"' in m.get("content", "") for m in messages):
                return tool_message("read_evidence", {"evidence_id": "ev_injury"})
            return tool_message("submit_answer", submission())
        with patch.object(agent, "request_completion", new=provider):
            await asyncio.wait_for(agent.run_agent(store, "run_one", asyncio.Event()), 2)
        self.assertEqual(store.get("runs", "run_one")["status"], "completed")
        self.assertTrue(any(key.startswith("agent_") for key in rounds))
        self.assertFalse(runtime_for(store).snapshot()["active"])

    async def test_parent_cancel_waits_for_child_provider_cleanup(self):
        store = FakeStore()
        store.new_run()
        entered, cleaned = asyncio.Event(), asyncio.Event()
        parent_round = 0
        async def provider(config, messages, **options):
            nonlocal parent_round
            if config["_session_id"].startswith("agent_"):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
            parent_round += 1
            if parent_round == 1:
                return tool_message("spawn_agent", {"task": "查阅训练状态"})
            return tool_message("wait_agents", {"timeout_seconds": 60})
        cancel = asyncio.Event()
        with patch.object(agent, "request_completion", new=provider):
            task = asyncio.create_task(agent.run_agent(store, "run_one", cancel))
            try:
                await asyncio.wait_for(entered.wait(), 1)
            finally:
                cancel.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(store.get("runs", "run_one")["status"], "cancelled")
        self.assertFalse(store._model_harness.snapshot()["active"])
        self.assertFalse(runtime_for(store).snapshot()["active"])

    async def test_spawn_cannot_expand_article_scope_or_tools(self):
        import time
        store = FakeStore()
        run = store.new_run(report_id="report_one", input_evidence_ids=["ev_injury"], allow_network=False)
        local = LocalTools(store, run, store.settings())
        delegation = Delegation(store, run, local, store.settings(), asyncio.Event(), time.monotonic() + 5)
        local.delegation = delegation
        try:
            with self.assertRaises(ToolError):
                await delegation.call("spawn_agent", {"task": "扩大来源", "evidence_ids": ["ev_transfer"]})
            with self.assertRaises(ToolError):
                await delegation.call("spawn_agent", {"task": "扩大联网", "tools": ["research_news"]})
            with self.assertRaises(ToolError):
                await delegation.call("spawn_agent", {"task": "改成复盘", "skill": "match_review"})
        finally:
            await delegation.close("completed")

    async def test_control_acknowledgements_never_return_old_private_results(self):
        import time
        store = FakeStore()
        run = store.new_run()
        local = LocalTools(store, run, store.settings())
        delegation = Delegation(store, run, local, store.settings(), asyncio.Event(), time.monotonic() + 5)
        local.delegation = delegation
        async def child(context):
            return {"answer": "PRIVATE_OLD_RESULT", "source_versions": {"ev_injury": source_version(store.get("evidence", "ev_injury"))}}
        try:
            handle = delegation.runtime.spawn(run["id"], execute=child, payload={})
            cursor = delegation.runtime.inspect(run["id"])["cursor"]
            await delegation.runtime.wait(run["id"], [handle["id"]], after=cursor, timeout=1)
            source = store.get("evidence", "ev_injury")
            store.put("evidence", {**source, "private": True})
            listed = await delegation.call("list_agents", {})
            self.assertNotIn("PRIVATE_OLD_RESULT", json.dumps(listed))
            cancelled = await delegation.call("cancel_agent", {"agent_id": handle["id"]})
            self.assertNotIn("PRIVATE_OLD_RESULT", json.dumps(cancelled))
            continued = await delegation.call("send_agent", {"agent_id": handle["id"], "message": "只查可见资料"})
            self.assertNotIn("PRIVATE_OLD_RESULT", json.dumps(continued))
        finally:
            await delegation.close("completed")

    async def test_failed_child_can_continue_and_release_context(self):
        import time
        from server.provider import AgentFailure
        store = FakeStore()
        run = store.new_run()
        local = LocalTools(store, run, store.settings())
        delegation = Delegation(store, run, local, store.settings(), asyncio.Event(), time.monotonic() + 5)
        local.delegation = delegation
        first = True
        seen_continuation = False
        async def provider(config, messages, **options):
            nonlocal first, seen_continuation
            if first:
                first = False
                raise AgentFailure("暂时不可用")
            seen_continuation |= "请重试训练状态" in json.dumps(messages, ensure_ascii=False)
            if not any(m.get("role") == "tool" for m in messages):
                return tool_message("read_evidence", {"evidence_id": "ev_injury"})
            return tool_message("submit_answer", submission())
        try:
            with patch.object(agent, "request_completion", new=provider):
                child = await delegation.call("spawn_agent", {"task": "查阅训练状态"})
                while True:
                    current = await delegation.call("wait_agents", {"agent_ids": [child["id"]], "timeout_seconds": 1})
                    if current["agents"][0]["status"] == "failed":
                        break
                acknowledgement = await delegation.call("send_agent", {"agent_id": child["id"], "message": "请重试训练状态"})
                self.assertEqual(acknowledgement["generation"], 2)
                cursor = current["cursor"]
                while True:
                    current = await delegation.call("wait_agents", {"agent_ids": [child["id"]], "after_cursor": cursor, "timeout_seconds": 1})
                    cursor = current["cursor"]
                    if current["agents"][0]["status"] in {"completed", "failed", "cancelled"}:
                        break
                self.assertEqual(current["agents"][0]["status"], "completed")
                self.assertTrue(seen_continuation)
                released = await delegation.call("release_agent", {"agent_id": child["id"]})
                self.assertTrue(released["context_released"])
                self.assertIsNone(released.get("result"))
        finally:
            await delegation.close("completed")

    async def test_new_message_during_model_request_is_processed_before_final_answer(self):
        import time
        store = FakeStore()
        run = store.new_run()
        local = LocalTools(store, run, store.settings())
        delegation = Delegation(store, run, local, store.settings(), asyncio.Event(), time.monotonic() + 5)
        local.delegation = delegation
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0
        seen = False
        async def provider(config, messages, **options):
            nonlocal count, seen
            count += 1
            if count == 1:
                entered.set()
                await release.wait()
                return tool_message("submit_answer", submission())
            seen |= "NEW_PARENT_INSTRUCTION" in json.dumps(messages)
            if count == 2:
                return tool_message("read_evidence", {"evidence_id": "ev_injury"})
            return tool_message("submit_answer", submission())
        try:
            with patch.object(agent, "request_completion", new=provider):
                child = await delegation.call("spawn_agent", {"task": "研究训练状态"})
                await asyncio.wait_for(entered.wait(), 1)
                await delegation.call("send_agent", {"agent_id": child["id"], "message": "NEW_PARENT_INSTRUCTION"})
                release.set()
                cursor = 0
                while True:
                    current = await delegation.call("wait_agents", {"agent_ids": [child["id"]], "after_cursor": cursor, "timeout_seconds": 1})
                    cursor = current["cursor"]
                    if current["agents"][0]["status"] in {"completed", "failed", "cancelled"}:
                        break
                self.assertEqual(current["agents"][0]["status"], "completed")
                self.assertEqual(current["agents"][0]["generation"], 1)
                self.assertTrue(seen)
                self.assertNotIn("NEW_PARENT_INSTRUCTION", json.dumps(store.data))
        finally:
            release.set()
            await delegation.close("completed")

    async def test_composite_tool_requires_its_read_dependency_at_creation(self):
        import time
        store = FakeStore()
        run = store.new_run()
        local = LocalTools(store, run, store.settings())
        delegation = Delegation(store, run, local, store.settings(), asyncio.Event(), time.monotonic() + 5)
        local.delegation = delegation
        try:
            for name in ("get_team_data", "read_article", "research_news"):
                with self.subTest(tool=name), self.assertRaisesRegex(ToolError, "read_evidence"):
                    await delegation.call("spawn_agent", {"task": "查阅资料", "tools": [name]})
            self.assertEqual(delegation.runtime.inspect(run["id"])["agents"], [])
        finally:
            await delegation.close("completed")

    async def test_a_background_tool_is_not_offered_to_a_subtask(self):
        # Photos and squad maintenance are the team page's work. Refusing them
        # at creation is the same outcome as before — no child does background
        # network work — but it is decided earlier, so the child never starts.
        import time
        store = FakeStore()
        run = store.new_run()
        local = LocalTools(store, run, store.settings())
        delegation = Delegation(store, run, local, store.settings(), asyncio.Event(), time.monotonic() + 5)
        local.delegation = delegation
        try:
            for name in ("maintain_team_data", "fetch_player_photos"):
                with self.subTest(tool=name), self.assertRaisesRegex(ToolError, "可用工具"):
                    await delegation.call("spawn_agent", {"task": "更新资料", "tools": [name]})
        finally:
            await delegation.close("completed")

    async def test_preview_scope_survives_child_and_grandchild_workflow_changes(self):
        store = FakeStore()
        store.new_run(skill="match_preview", match_id="match_one")
        stages = {}
        protected_children = set()
        denied_reads = set()
        async def provider(config, messages, **options):
            session = config["_session_id"]
            stage = stages.get(session, 0)
            tools = {tool["function"]["name"] for tool in options["tools"]}
            child = session.startswith("agent_")
            grandchild = any(message.get("role") == "user" and str(message.get("content", "")).startswith("孙任务核对训练") for message in messages)
            if child and stage == 0:
                self.assertNotIn("research_news", tools)
                self.assertNotIn("read_article", tools)
                protected_children.add(session)
                stages[session] = 1
                return tool_message("read_evidence", {"evidence_id": "ev_result"})
            if child and stage == 1:
                self.assertIn("error", json.loads(messages[-1]["content"]))
                denied_reads.add(session)
            if (not child and stage == 0) or (child and not grandchild and stage == 1):
                stages[session] = 2
                return tool_message("spawn_agent", {"task": "孙任务核对训练" if child else "子任务核对训练", "skill": "ask"})
            if not grandchild and stage == 2:
                stages[session] = 3
                return tool_message("wait_agents", {"timeout_seconds": 2})
            if not grandchild and stage == 3:
                outcome = json.loads(messages[-1]["content"])
                if not all(row["status"] == "completed" for row in outcome["agents"]):
                    return tool_message("wait_agents", {"after_cursor": outcome["cursor"], "timeout_seconds": 2})
            if stage != 4:
                stages[session] = 4
                return tool_message("read_evidence", {"evidence_id": "ev_injury"})
            return tool_message("submit_answer", submission())
        with patch.object(agent, "request_completion", new=provider):
            await asyncio.wait_for(agent.run_agent(store, "run_one", asyncio.Event()), 3)
        self.assertEqual(store.get("runs", "run_one")["status"], "completed")
        self.assertEqual(len(protected_children), 2)
        self.assertEqual(denied_reads, protected_children)
        self.assertFalse(runtime_for(store).snapshot()["active"])

    async def test_connection_probe_uses_shared_model_capacity(self):
        harness = ModelHarness(global_limit=1, provider_limit=1)
        entered, release = asyncio.Event(), asyncio.Event()
        config = FakeStore().settings()["llm"]
        async def occupied():
            entered.set()
            await release.wait()
        holding = asyncio.create_task(harness.request(config, occupied, owner_id="busy", role="chat", stage="answer"))
        await entered.wait()
        invoked = asyncio.Event()
        async def provider(*args, **kwargs):
            invoked.set()
            return {"content": "OK"}
        with patch.object(agent, "request_completion", new=provider):
            probe = asyncio.create_task(agent.test_connection(config, dispatcher=harness))
            try:
                await asyncio.sleep(0)
                self.assertFalse(invoked.is_set())
                release.set()
                await asyncio.wait_for(probe, 1)
                self.assertTrue(invoked.is_set())
                self.assertTrue(any(row["role"] == "probe" for row in harness.snapshot()["recent"]))
            finally:
                release.set()
                await holding
                if not probe.done():
                    probe.cancel()
                await asyncio.gather(probe, return_exceptions=True)


class DelegationAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_source_hides_previously_completed_answer(self):
        import httpx
        from server.main import create_app
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory) / "isolated.sqlite3")
            store = app.state.store
            fake = FakeStore()
            store.save_settings({**fake.settings(), "verification_enabled": False})
            store.put("evidence", fake.get("evidence", "ev_injury"))
            store.put("runs", fake.new_run())
            responses = [tool_message("read_evidence", {"evidence_id": "ev_injury"}), tool_message("submit_answer", submission())]
            try:
                async def provider(*args, **kwargs):
                    return responses.pop(0)
                with patch.object(agent, "request_completion", new=provider):
                    await agent.run_agent(store, "run_one", asyncio.Event())
                self.assertEqual(store.get("runs", "run_one")["status"], "completed")
                self.assertEqual(store.get("runs", "run_one")["evidence_ids"], ["ev_injury"])
                self.assertTrue(store.list("agent_tasks"))
                source = store.get("evidence", "ev_injury")
                store.put("evidence", {**source, "text": "Correction: the midfielder is healthy and training."})
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                    response = await client.get("/api/runs/run_one")
                    self.assertEqual(response.status_code, 200)
                    view = response.json()
                    self.assertTrue(view.get("source_stale"))
                    self.assertIsNone(view.get("structured_answer"))
                    self.assertNotIn("因伤暂未参加训练", view["answer"])
                    self.assertNotIn("evidence_versions", view)
                    store.put("evidence", {**source, "private": True})
                    hidden = (await client.get("/api/runs/run_one")).json()
                    self.assertTrue(hidden["source_hidden"])
                    self.assertNotIn("evidence_versions", hidden)
                    self.assertNotIn(source["title"], json.dumps(hidden, ensure_ascii=False))
                    diagnostic = (await client.get("/api/harness")).json()
                    self.assertIn("agents", diagnostic)
                    self.assertNotIn("Medical update", json.dumps(diagnostic))
            finally:
                await runtime_for(store).shutdown()
                store.close()
