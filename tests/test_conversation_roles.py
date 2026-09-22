"""Role isolation and durable history, using temporary data and no remote calls."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from server import model_config
from server.main import create_app
from tests.test_model_profiles import profile


class ConversationRoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.directory.name) / "test.sqlite3")
        self.store = self.app.state.store
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")
        model_config.save_profile(self.store, profile())
        model_config.save_profile(self.store, profile(id="beta", api_key="other-test-key", base_url="https://beta.example/v1"))

    async def asyncTearDown(self):
        tasks = [entry[0] for entry in self.app.state.run_tasks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.app.state.personal.close()
        await self.app.state.team_maintenance.close()
        await self.client.aclose()
        self.store.close()
        self.directory.cleanup()

    def turn(self, index, **updates):
        row = {"id": f"run_{index}", "conversation_id": "conversation_old", "team_id": "arsenal", "mode": "live",
               "question": f"问题 {index}", "answer": "已保存的回答", "status": "completed", "skill": "ask",
               "provider_id": "alpha", "model": "one", "created_at": f"2026-09-16T{index // 60:02d}:{index % 60:02d}:00Z",
               "finished_at": None, "evidence_ids": [], "steps": [], "warnings": [], "spoiler": False}
        row.update(updates)
        return self.store.put("runs", row)

    async def test_role_assignments_are_independent_and_credentials_write_only(self):
        result = await self.client.put("/api/models/roles", json={"roles": {
            "feed": {"provider_id": "beta", "model": "two"}, "team": {"provider_id": "alpha", "model": "two"}}})
        self.assertEqual(result.status_code, 200, result.text)
        view = result.json()
        self.assertIsNone(view["roles"]["chat"])
        self.assertEqual(view["resolved_roles"]["chat"], {"provider_id": "alpha", "model": "one"})
        self.assertNotIn("other-test-key", result.text)
        self.assertEqual(model_config.role_settings(self.store, "feed")["llm"]["api_key"], "other-test-key")
        self.assertEqual(model_config.role_settings(self.store, "team")["llm"]["model"], "two")
        self.assertEqual(self.store.settings()["llm"]["model"], "one")
        await self.client.put("/api/models/roles", json={"roles": {"feed": None}})
        self.assertEqual(model_config.role_selection(self.store, "feed"), {"provider_id": "alpha", "model": "one"})

    async def test_invalid_assignment_is_atomic_and_unknown_roles_rejected(self):
        result = await self.client.put("/api/models/roles", json={"roles": {
            "chat": {"provider_id": "beta", "model": "one"}, "feed": {"provider_id": "alpha", "model": "missing"}}})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(model_config.role_selection(self.store, "chat")["provider_id"], "alpha")
        self.assertEqual((await self.client.put("/api/models/roles", json={"roles": {"unknown": None}})).status_code, 422)

    async def test_new_turn_uses_role_but_existing_conversation_keeps_model(self):
        await self.client.put("/api/models/roles", json={"roles": {
            "chat": {"provider_id": "beta", "model": "two"}, "team": {"provider_id": "alpha", "model": "two"}}})
        self.turn(1)
        with patch("server.agent.run_agent", new=AsyncMock()):
            old = await self.client.post("/api/runs", json={"mode": "live", "team_id": "arsenal", "question": "继续", "conversation_id": "conversation_old"})
            new = await self.client.post("/api/runs", json={"mode": "live", "team_id": "arsenal", "question": "新问题"})
            team = await self.client.post("/api/runs", json={"mode": "live", "team_id": "arsenal", "question": "球队介绍", "skill": "team_maintenance"})
            await asyncio.sleep(0)
        self.assertEqual(old.json()["provider_id"], "alpha", old.text)
        self.assertEqual((new.json()["provider_id"], new.json()["model"]), ("beta", "two"))
        self.assertEqual((team.json()["provider_id"], team.json()["model_role"]), ("alpha", "team"))

    async def test_history_is_complete_beyond_dashboard_cap_and_has_pagination(self):
        for index in range(130):
            self.turn(index)
        self.turn(131, conversation_id="conversation_new", status="failed", answer="", error="请求超时")
        self.turn(132, conversation_id="background", internal_report=True)
        self.turn(133, conversation_id="tracked", tracking_id="tracking_1")
        result = (await self.client.get("/api/conversations?team_id=arsenal&limit=1")).json()
        self.assertEqual(result["conversations"][0]["id"], "conversation_new")
        self.assertEqual(result["conversations"][0]["last_status"], "failed")
        older = (await self.client.get("/api/conversations", params={"team_id": "arsenal", "limit": 1, "cursor": result["next_cursor"]})).json()
        self.assertEqual(older["conversations"][0]["run_count"], 130)
        detail = (await self.client.get("/api/conversations/conversation_old")).json()
        self.assertEqual(len(detail["runs"]), 130)
        self.assertEqual(detail["runs"][0]["question"], "问题 0")
        self.assertEqual((await self.client.get("/api/conversations/background")).status_code, 404)
        self.assertEqual((await self.client.get("/api/conversations/tracked")).status_code, 404)
        self.assertEqual((await self.client.get("/api/conversations?cursor=broken")).status_code, 400)

    async def test_cannot_join_another_team_or_background_or_busy_conversation(self):
        self.turn(1)
        self.turn(2, conversation_id="background", internal_report=True)
        self.turn(3, conversation_id="busy", status="running")
        for team, identity, code in [("barcelona", "conversation_old", 400), ("arsenal", "background", 400), ("arsenal", "busy", 409)]:
            result = await self.client.post("/api/runs", json={"mode": "live", "team_id": team, "question": "继续", "conversation_id": identity})
            self.assertEqual(result.status_code, code, result.text)

    async def test_unavailable_sources_and_other_provider_secrets_never_leak(self):
        self.store.put("evidence", {"id": "ev_private", "mode": "live", "team_id": "arsenal", "private": True, "text": "私有内容"})
        self.turn(1, evidence_ids=["ev_private"], answer="私有内容", steps=[{"detail": "私有内容"}])
        response = await self.client.get("/api/conversations/conversation_old")
        self.assertNotIn("私有内容", response.text)
        self.turn(2, conversation_id="errors", error="other-test-key")
        self.assertNotIn("other-test-key", (await self.client.get("/api/conversations/errors")).text)

    async def test_parent_run_cannot_smuggle_background_or_other_conversation_context(self):
        self.turn(1, report_id="report_hidden", internal_report=True)
        self.turn(2, conversation_id="another")
        self.turn(3, conversation_id="current")
        for parent in ("run_1", "run_2"):
            result = await self.client.post("/api/runs", json={"mode": "live", "team_id": "arsenal", "question": "继续",
                "conversation_id": "current", "parent_run_id": parent})
            self.assertEqual(result.status_code, 400, result.text)

    async def test_role_remains_configured_when_default_provider_is_removed(self):
        await self.client.put("/api/models/roles", json={"roles": {"chat": {"provider_id": "beta", "model": "two"}}})
        await self.client.delete("/api/model-providers/alpha")
        setup = (await self.client.get("/api/state")).json()["setup"]
        self.assertTrue(setup["model_configured"])
        self.assertEqual(setup["model_roles_configured"], {"chat": True, "feed": False, "team": False, "verify": False})

    async def test_removed_provider_updates_roles_without_rebinding_old_turns(self):
        await self.client.put("/api/models/roles", json={"roles": {"feed": {"provider_id": "beta", "model": "two"}}})
        self.turn(1, provider_id="beta")
        response = await self.client.delete("/api/model-providers/beta")
        self.assertIsNone(response.json()["roles"]["feed"])
        self.assertEqual(self.store.get("runs", "run_1")["provider_id"], "beta")
        result = await self.client.post("/api/runs", json={"mode": "live", "team_id": "arsenal", "question": "继续", "conversation_id": "conversation_old"})
        self.assertEqual(result.status_code, 400)

    async def test_legacy_conversation_uses_only_unambiguous_saved_model_match(self):
        # Both configured providers expose the same ID: do not guess a provider.
        self.turn(1, provider_id=None)
        request = {"mode": "live", "team_id": "arsenal", "question": "继续", "conversation_id": "conversation_old"}
        self.assertEqual((await self.client.post("/api/runs", json=request)).status_code, 409)
        await self.client.delete("/api/model-providers/beta")
        view = (await self.client.get("/api/conversations/conversation_old")).json()
        self.assertEqual(view["conversation"]["provider_id"], "alpha")
        with patch("server.agent.run_agent", new=AsyncMock()):
            resumed = await self.client.post("/api/runs", json=request)
            await asyncio.sleep(0)
        self.assertEqual(resumed.json()["provider_id"], "alpha", resumed.text)
        self.assertIsNone(self.store.get("runs", "run_1").get("provider_id"))


if __name__ == "__main__":
    unittest.main()
