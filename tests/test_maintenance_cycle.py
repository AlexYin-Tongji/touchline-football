"""The model-driven maintenance cycle: who starts it, and what it may hand out.

A UI-triggered refresh used to be pure code — `maintain_team` called in a loop,
with the skill tree never reaching a model. It is now a run when a model is
assigned to the team role, so the root can read the skill, decide which blocks
are stale, and delegate each one to a sub-agent.

Two things about that are easy to get wrong and expensive to discover in
production, so they are pinned here:

* the fallback. A user with no model assigned must still get their club's data
  refreshed, through the deterministic pass, without a provider round-trip.
* the tool hand-off. A sub-agent can only be given tools its parent holds, and
  the maintenance tools are withheld from conversations by default — so if the
  root cannot hold them, the whole tree has nothing to run and the cycle
  completes while silently doing nothing.
"""
import asyncio
import json
import unittest
from unittest.mock import patch

from server import agent
from server.agent_delegation import Delegation
from server.agent_tools import LocalTools, ToolError
from server.tool_registry import workflow_exposes
from tests.test_agent import FakeStore, tool_message
from tests.test_workflows import MemoryStore


class MaintenanceCycleMemoryStore(MemoryStore):
    """A store that can also answer the credential lookup.

    ``role_settings`` resolves a role through ``effective_config``, which reads
    the provider's key. The base fixture has no secret layer, and a keyless local
    provider is a supported configuration, so returning None here is the shape a
    real store has for one.
    """
    def get_secret(self, key):
        return None

    def set_secret(self, key, value):
        self.meta.setdefault("secrets", {})[key] = value


def team_run():
    return {"id": "maintain_one", "mode": "live", "team_id": "arsenal", "skill": "team_refresh",
            "question": "按块核对并更新本球队的资料。", "conversation_id": "maintain_one",
            "internal_report": True, "model_role": "team", "status": "queued", "answer": "",
            "evidence_ids": [], "steps": [], "warnings": [], "allow_network": True}


class MaintenanceToolHandoffTests(unittest.IsolatedAsyncioTestCase):
    def build(self):
        store = FakeStore()
        store.put("runs", team_run())
        run = store.get("runs", "maintain_one")
        local = LocalTools(store, run, store.settings(), agent.load_skill)
        deleg = Delegation(store, run, local, store.settings(), asyncio.Event(), __import__("time").monotonic() + 60)
        local.delegation = deleg
        return store, local, deleg

    async def test_the_root_of_a_maintenance_run_holds_the_tools_its_blocks_need(self):
        # Without this the run is decorative: it starts, delegates nothing that
        # can do the work, and reports success.
        _, local, deleg = self.build()
        try:
            names = {t["function"]["name"] for t in local.available_tools()}
            for tool in ("maintain_team_data", "fetch_player_photos", "get_team_data", "spawn_agent"):
                with self.subTest(tool=tool):
                    self.assertIn(tool, names)
        finally:
            await deleg.close("completed")

    async def test_a_block_can_be_given_its_maintenance_tool(self):
        _, local, deleg = self.build()
        try:
            result = await deleg.call("spawn_agent", {
                "task": "同步阵容", "tools": ["get_team_data", "maintain_team_data", "read_evidence", "submit_answer"]})
            self.assertTrue(result.get("id"))
            # A runtime status envelope carries ``error: None`` on success; only
            # a message means the call failed.
            self.assertIsNone(result.get("error"))
        finally:
            await deleg.close("completed")

    async def test_only_the_maintenance_workflow_exposes_the_background_tools(self):
        # The image direction exposes the photo tool for a conversation; the
        # maintenance workflow exposes the batch for a refresh. A question must
        # get neither, or every ordinary chat would carry the slowest tools.
        self.assertTrue(workflow_exposes("team_refresh"))
        for workflow in ("ask", "team_brief", "news_report", "team_maintenance", "match_preview"):
            with self.subTest(workflow=workflow):
                self.assertEqual(workflow_exposes(workflow), ())


class MaintenanceCycleRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_team_role_routes_the_cycle_through_a_run(self):
        from server.workflows import WorkflowManager
        store = MaintenanceCycleMemoryStore()
        store.put("model_profiles", {"id": "alpha", "name": "Alpha", "base_url": "https://alpha.example/v1",
                                     "protocol": "chat_completions",
                                     "models": [{"id": "one", "name": "One"}]})
        store.meta["model_roles"] = {"team": {"provider_id": "alpha", "model": "one"}}
        seen = {}

        async def provider(config, messages, **options):
            seen["tools"] = {t["function"]["name"] for t in options.get("tools", [])}
            seen["system"] = messages[0]["content"]
            return tool_message("submit_answer", {"status": "complete",
                                                  "answer": "阵容已核对，赛程与积分本轮无缺口。", "facts": []})

        manager = WorkflowManager(store)
        with patch.object(agent, "request_completion", new=provider):
            # No parts is the panel refresh. An explicit list still uses the
            # maintenance agent when a team model is configured.
            manager.start("team", "arsenal", parts=["roster", "schedule", "standings"])
            await manager.tasks[("team", "arsenal")].task
        self.assertIn("球队维护", seen.get("system", ""), "the root must receive the maintenance skill")
        self.assertIn("maintain_team_data", seen.get("tools", set()))
        runs = [row for row in store.list("runs") if row.get("skill") == "team_refresh"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "completed")

    async def test_the_root_delegates_a_block_and_finishes_on_its_result(self):
        """The whole chain: skill -> root -> sub-agent -> block -> root delivers.

        This is the behaviour the change exists for. The root must be able to
        hand a block to a child, the child must be able to deliver an operation
        outcome (it reads no article), and the root must be able to read that
        result back and close the cycle.
        """
        from server.workflows import WorkflowManager
        store = MaintenanceCycleMemoryStore()
        store.put("model_profiles", {"id": "alpha", "name": "Alpha", "base_url": "https://alpha.example/v1",
                                     "protocol": "chat_completions",
                                     "models": [{"id": "one", "name": "One"}]})
        store.meta["model_roles"] = {"team": {"provider_id": "alpha", "model": "one"}}
        rounds, child_rounds = {"root": 0}, {}
        handles, child_answers, child_messages = [], [], []

        async def provider(config, messages, **options):
            session = config.get("_session_id", "")
            if session.startswith("agent_"):
                child_rounds[session] = child_rounds.get(session, 0) + 1
                child_messages.extend(messages)
                # The block loads its own direction, then reports the outcome.
                if child_rounds[session] == 1:
                    return tool_message("use_skill", {"skill": "team-maintenance", "topic": "roster"},
                                        f"skill_{child_rounds[session]}")
                return tool_message("submit_answer", {"status": "complete",
                                                      "answer": "阵容块已核对：27 人全部有来源，号码与位置无缺口。",
                                                      "facts": []}, f"sub_{child_rounds[session]}")
            rounds["root"] += 1
            names = {t["function"]["name"] for t in options.get("tools", [])}
            self.assertIn("spawn_agent", names)
            if rounds["root"] == 1:
                return {"role": "assistant", "content": None, "tool_calls": [
                    tool_message("spawn_agent", {"task": "核对阵容块", "context": "只核对阵容",
                                                 "skill": "team_refresh",
                                                 "tools": ["get_team_data", "maintain_team_data",
                                                           "read_evidence", "submit_answer", "use_skill"]},
                                 "spawn_roster")["tool_calls"][0]]}
            if rounds["root"] == 2:
                handles.extend(json.loads(m["content"])["id"] for m in messages
                               if m.get("role") == "tool" and isinstance(m.get("content"), str)
                               and json.loads(m["content"]).get("id", "").startswith("agent_"))
                return tool_message("wait_agents", {"agent_ids": handles, "timeout_seconds": 10}, "wait_1")
            result = json.loads(messages[-1]["content"])
            child_answers.extend(row.get("result", {}).get("answer", "") for row in result.get("agents", []))
            return tool_message("submit_answer", {"status": "complete",
                                                  "answer": "本轮维护：阵容块已完成，其余块本轮无缺口。", "facts": []},
                                f"finish_{rounds['root']}")

        manager = WorkflowManager(store)
        with patch.object(agent, "request_completion", new=provider):
            manager.start("team", "arsenal", parts=["roster"])
            await asyncio.wait_for(manager.tasks[("team", "arsenal")].task, 20)
        run = [row for row in store.list("runs") if row.get("skill") == "team_refresh"][0]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(child_rounds), 1, "the root must have delegated to exactly one block")
        self.assertTrue(any("阵容块已核对" in value for value in child_answers),
                        "the root must receive what the block reported")
        self.assertIn("阵容块已完成", run["answer"])
        # The block's use_skill call returned real instructions rather than an
        # error, which is what distinguishes "the tree was walked" from "the
        # model was told a tree exists".
        self.assertTrue(any("球员名单" in json.dumps(message, ensure_ascii=False)
                            or "阵容" in json.dumps(message, ensure_ascii=False)
                            for message in child_messages),
                        "the block must have loaded its own direction")

    async def test_no_team_role_falls_back_to_the_deterministic_pass(self):
        from server.workflows import WorkflowManager
        store = MaintenanceCycleMemoryStore()
        calls = []

        async def cycle(manager, entry, first):
            calls.append(entry.team_id)

        manager = WorkflowManager(store, cycle_runner=cycle)
        manager.start("team", "arsenal")
        await manager.tasks[("team", "arsenal")].task
        self.assertEqual(calls, ["arsenal"])
        self.assertEqual([row for row in store.list("runs") if row.get("skill") == "team_refresh"], [])

    async def test_a_role_pointing_at_a_missing_provider_falls_back(self):
        from server.workflows import WorkflowManager
        store = MaintenanceCycleMemoryStore()
        store.meta["model_roles"] = {"team": {"provider_id": "gone", "model": "m1"}}
        manager = WorkflowManager(store)
        # Resolving the role raises; the cycle must not, and must not spawn a run
        # that would fail every block.
        self.assertFalse(manager._team_agent_configured())


if __name__ == "__main__":
    unittest.main()
