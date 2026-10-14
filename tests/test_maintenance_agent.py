"""The maintenance delivery shape, and which runs are allowed to use it.

A maintenance run reports an operation: which parts updated, which are still
missing, and what each one's data time is. Its blocks call maintain_team_data
and get rows back — no article is read, so there is no passage to quote and no
evidence id to cite. The submit contract was written for the other two kinds of
task, both of which make claims about the world, and the rule that a claim needs
text read this round is what keeps them honest.

These tests exist because the exception is easy to widen by accident. Each one
states the boundary from both sides: the maintenance shape is allowed, and every
other shape is still refused.
"""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

from server import agent
from server.agent_delegation import Delegation
from server.agent_tools import LocalTools, ToolError, operation_delivery
from tests.test_agent import FakeStore, tool_message


def run_for(skill, **patches):
    base = {"id": "run_one", "mode": "live", "team_id": "arsenal", "skill": skill,
            "question": "更新球队资料", "conversation_id": "conversation_one", "status": "queued",
            "answer": "", "evidence_ids": [], "warnings": [], "steps": [], "error": None,
            "created_at": "2026-10-13T00:00:00Z", "finished_at": None, "model": ""}
    base.update(patches)
    return base


def operation_submission():
    """What a maintenance block actually has to say: an outcome, not a claim."""
    return {"status": "complete",
            "answer": "阵容已同步，7 名球员的号码与位置更新；赛程仍缺两轮，来源本轮不可用。",
            "facts": []}


class OperationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def attempt(self, skill, payload, *, settings=None, internal=False, allowlist=None):
        store = FakeStore()
        run = run_for(skill, internal_agent=internal,
                      tool_allowlist=allowlist or ["get_team_data", "maintain_team_data", "read_evidence", "submit_answer"])
        store.put("runs", run)
        local = LocalTools(store, run, {**store.settings(), **(settings or {})}, None)
        return local, local.call("submit_answer", copy.deepcopy(payload))

    def test_a_maintenance_block_delivers_an_outcome_without_quoting_anything(self):
        local, result = self.attempt("team_refresh", operation_submission())
        self.assertTrue(result["submitted"])
        self.assertEqual(result["facts_count"], 0)
        self.assertEqual(local.submission["status"], "complete")
        # The outcome text is what the reader gets; no bracketed ids were needed.
        self.assertIn("阵容已同步", local.submission["answer"])
        self.assertEqual(local.submission["evidence_ids"], [])

    def test_a_question_still_cannot_conclude_without_reading_anything(self):
        # The rule the maintenance shape is exempt from, stated for the task it
        # was written for. If this ever passes, the exemption leaked.
        with self.assertRaisesRegex(ToolError, "至少需要一项实际已读原文支持的事实"):
            self.attempt("ask", operation_submission())

    def test_a_news_report_still_cannot_conclude_without_reading_anything(self):
        with self.assertRaisesRegex(ToolError, "实际内容|至少需要一项"):
            self.attempt("news_report", {**operation_submission(), "title": "球队资料更新情况说明"})

    def test_only_the_maintenance_workflow_is_an_operation(self):
        self.assertTrue(operation_delivery("team_refresh"))
        for skill in ("ask", "news_report", "team_brief", "team_maintenance",
                      "match_preview", "match_review", "transfer_check"):
            with self.subTest(skill=skill):
                self.assertFalse(operation_delivery(skill))


class SubAgentVerificationTests(unittest.IsolatedAsyncioTestCase):
    def build(self, verification, *, internal=False):
        store = FakeStore()
        run = run_for("team_refresh", internal_agent=internal,
                      tool_allowlist=["get_team_data", "maintain_team_data", "submit_answer"])
        store.put("runs", run)
        settings = {**store.settings(), "verification_enabled": verification}
        return store, run, LocalTools(store, run, settings, None)

    def test_a_subtask_inherits_the_runs_verification_posture(self):
        # Being a sub-agent is not what exempts a task from verification any
        # more. The exemption belongs to the maintenance *kind* of work, which
        # has no claims to check in the first place.
        for verification, expected in ((True, True), (False, False)):
            with self.subTest(verification=verification):
                _, _, local = self.build(verification, internal=True)
                self.assertEqual(local.verification_enabled, expected)

    def test_an_operation_child_does_not_request_a_paid_check_round(self):
        # Zero facts means nothing to check: prepare_claims returns no payload,
        # so verify_submission reports insufficient without calling a model.
        # This is what makes inheriting verification free for a maintenance
        # tree — the paid call only happens where there is something to check.
        from server import verification
        _, _, local = self.build(True, internal=True)
        _, payload = verification.prepare_claims(local, operation_submission())
        self.assertIsNone(payload)

    def test_an_operation_child_delivers_while_verification_is_on(self):
        store, _, local = self.build(True, internal=True)
        self.assertTrue(local.verification_enabled)
        result = local.call("submit_answer", operation_submission())
        self.assertTrue(result["submitted"])


class MaintenanceRunIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_maintenance_run_completes_without_a_provider_check_round(self):
        """End to end: the run delivers, and no checker request is ever made."""
        store = FakeStore()
        store.config["verification_enabled"] = True
        store.put("runs", run_for("team_refresh", status="queued"))
        rounds = []
        checked = []

        async def provider(config, messages, **options):
            rounds.append(config.get("_session_id"))
            stage = options.get("stage")
            if stage in {"verify", "coverage"} or config.get("_session_id", "").endswith("_repair"):
                checked.append(stage)
                return tool_message("submit_answer", operation_submission())
            if len(rounds) == 1:
                return tool_message("submit_answer", operation_submission())
            return tool_message("submit_answer", operation_submission())

        with patch.object(agent, "request_completion", new=provider):
            await asyncio.wait_for(agent.run_agent(store, "run_one", asyncio.Event()), 5)
        run = store.get("runs", "run_one")
        self.assertEqual(run["status"], "completed")
        self.assertIn("阵容已同步", run["answer"])
        self.assertEqual(checked, [], "an operation with no facts must not start a checker request")
        self.assertEqual(run["delivery"]["facts_count"], 0)


if __name__ == "__main__":
    unittest.main()
