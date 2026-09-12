"""Club upgrades and extracted-source memory survive without crossing scopes."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from server import agent
from server.main import create_app
from server.seed import seed
from server.storage import Store
from server.team_profiles import archive_run_knowledge, knowledge_for_team
from tests.test_agent import FakeStore, tool_message
from tests.test_agent_delivery import submission


class ProfilePersistenceTests(unittest.TestCase):
    def test_upgrade_preserves_user_values_and_adds_local_images_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "club.sqlite3"
            store = Store(path)
            store.put("teams", {"id": "arsenal", "name": "我的阿森纳", "crest_url": "/assets/custom.png", "city": ""})
            store.save_settings({"followed_teams": ["arsenal"], "mode": "live"})
            seed(store)
            arsenal = store.get("teams", "arsenal")
            self.assertEqual(arsenal["name"], "我的阿森纳")
            self.assertEqual(arsenal["crest_url"], "/assets/custom.png")
            self.assertEqual(arsenal["city"], "")
            self.assertEqual(arsenal["founded"], 1886)
            self.assertTrue(arsenal["profile_sources"])
            for team in store.list("teams"):
                self.assertTrue(team["crest_attribution"])
                if team["id"] != "arsenal":
                    self.assertTrue((Path(__file__).resolve().parents[1] / "public" / team["crest_url"].lstrip("/")).is_file())
            self.assertEqual(store.list("evidence", "live"), [])
            seed(store)
            self.assertEqual(store.get("teams", "arsenal"), arsenal)
            store.close()
            reopened = Store(path)
            self.assertEqual(reopened.get("teams", "arsenal"), arsenal)
            self.assertEqual(reopened.settings()["mode"], "live")
            reopened.close()

    def test_profile_endpoint_updates_custom_clubs_and_validates_image_links(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.sqlite3"
            with TestClient(create_app(path)) as client:
                profile = client.get("/api/teams/arsenal/profile?mode=live").json()
                self.assertEqual(profile["library"]["knowledge_count"], 0)
                self.assertEqual(profile["team"]["crest_url"], "/assets/arsenal.svg")
                team = client.post("/api/teams", json={"name": "自建球队"}).json()
                result = client.patch(f"/api/teams/{team['id']}/profile", json={
                    "city": "上海", "description": "我的社区球队。", "founded": 2021,
                    "crest_url": "/assets/community.png", "profile_sources": [{"name": "球队主页", "url": "https://example.com/team"}]})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["city"], "上海")
                for value in ("javascript:alert(1)", "http://127.0.0.1/logo.png", "/assets/../secret.png", "/assets/x.html"):
                    self.assertEqual(client.patch(f"/api/teams/{team['id']}/profile", json={"crest_url": value}).status_code, 422)
                self.assertEqual(client.get("/api/teams/missing/profile").status_code, 404)
                self.assertEqual(client.get("/api/teams/arsenal/profile?mode=unknown").status_code, 422)
            with TestClient(create_app(path)) as client:
                self.assertEqual(client.get(f"/api/teams/{team['id']}/profile").json()["team"]["city"], "上海")


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.run = self.store.new_run(status="completed", structured_answer=submission(),
            evidence_ids=["ev_injury"], read_evidence_ids=["ev_injury"], finished_at="2026-09-09T11:00:00Z")
        self.cache = {"ev_injury": self.store.get("evidence", "ev_injury")}

    def archive(self, run=None):
        return archive_run_knowledge(self.store, run or self.run, read_cache=self.cache)

    def test_memory_deduplicates_claims_retains_sources_and_never_saves_analysis(self):
        original = self.archive()
        rerun = dict(self.run, id="run_two")
        self.assertEqual(self.archive(rerun), original)
        notes = self.store.list("knowledge")
        self.assertEqual(len(notes), 1)
        note = notes[0]
        self.assertEqual(note["run_ids"], ["run_one", "run_two"])
        self.assertEqual(note["support"], "citation_checked")
        self.assertEqual(note["sources"][0]["published_at"], "2026-08-31T09:00:00Z")
        self.assertNotIn("analysis", note)
        self.assertNotIn("summary", note)
        self.assertEqual(knowledge_for_team(self.store, "arsenal")[0]["id"], original[0])
        self.assertEqual(knowledge_for_team(self.store, "liverpool"), [])
        self.assertEqual(knowledge_for_team(self.store, "arsenal", mode="demo"), [])
        self.assertEqual(knowledge_for_team(self.store, "arsenal", allowed_evidence_ids=[]), [])

    def test_unread_cross_scope_and_unfinished_answers_never_enter_memory(self):
        for changes in ({"status": "failed"}, {"status": "cancelled"}, {"read_evidence_ids": []},
                        {"evidence_ids": []}, {"mode": "demo"}, {"team_id": "liverpool"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.archive(dict(self.run, **changes)), [])
        self.assertEqual(archive_run_knowledge(self.store, self.run), [])
        self.assertEqual(self.store.list("knowledge"), [])

    def test_checked_memory_only_keeps_supported_facts_even_after_fact_reordering(self):
        fact = self.run["structured_answer"]["facts"][0]
        checked = {"status": "partial", "claims": [{"id": "fact_2", "text": fact["title"] + "：" + fact["detail"], "verdict": "supported"}]}
        self.assertEqual(len(self.archive(dict(self.run, verification=checked))), 1)
        self.assertEqual(self.store.list("knowledge")[0]["support"], "source_supported")
        self.store.data["knowledge"] = {}
        checked["claims"][0]["verdict"] = "unsupported"
        self.assertEqual(self.archive(dict(self.run, verification=checked)), [])

    def test_source_changes_or_deletion_invalidate_old_memory_and_read_snapshot(self):
        self.archive()
        source = self.store.get("evidence", "ev_injury")
        source["text"] += " The original report was corrected."
        self.store.put("evidence", source)
        self.assertEqual(knowledge_for_team(self.store, "arsenal"), [])
        self.assertEqual(self.archive(), [])
        self.store.data["evidence"].pop("ev_injury")
        self.assertEqual(knowledge_for_team(self.store, "arsenal"), [])

    def test_spoiler_memory_is_hidden_until_user_turns_off_spoiler_protection(self):
        self.archive(dict(self.run, spoiler=True))
        self.assertEqual(knowledge_for_team(self.store, "arsenal"), [])
        self.assertEqual(len(knowledge_for_team(self.store, "arsenal", spoiler_free=False)), 1)

    def test_memory_survives_reopening_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "knowledge.sqlite3"
            store = Store(path)
            for collection in ("teams", "evidence"):
                for item in self.store.list(collection):
                    store.put(collection, item)
            identifiers = archive_run_knowledge(store, self.run, read_cache=self.cache)
            store.close()
            reopened = Store(path)
            self.assertEqual(knowledge_for_team(reopened, "arsenal")[0]["id"], identifiers[0])
            reopened.close()


class AgentKnowledgeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_answer_is_automatically_saved_as_source_linked_memory(self):
        store = FakeStore()
        store.new_run()
        with patch.object(agent, "request_completion", new=AsyncMock(side_effect=[
                tool_message("read_evidence", {"evidence_id": "ev_injury"}), tool_message("submit_answer", submission())])):
            await agent.run_agent(store, "run_one", asyncio.Event())
        run = store.get("runs", "run_one")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(run["knowledge_ids"]), 1)
        self.assertEqual(store.get("knowledge", run["knowledge_ids"][0])["evidence_ids"], ["ev_injury"])


if __name__ == "__main__":
    unittest.main()
