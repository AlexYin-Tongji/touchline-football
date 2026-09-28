"""Audit and credential endpoints remain behind existing application boundaries."""
from server import decisions, providers
from tests.support import IsolatedAppCase


class CollectionApiTests(IsolatedAppCase):
    def test_team_history_is_available_without_reading_other_teams(self):
        identity = decisions.begin(self.app.state.store, "arsenal", {}, parts=["roster"],
                                   trigger="automatic", reason="阵容过期。")
        response = self.client.get("/api/teams/arsenal/decisions?limit=3")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"][0]["id"], identity)
        self.assertEqual(self.client.get("/api/teams/not-a-team/decisions").status_code, 404)

    def test_source_key_is_encrypted_and_never_returned(self):
        response = self.client.put("/api/data-sources/football_data/credential", json={"api_key": "test-private-key"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("test-private-key", response.text)
        self.assertEqual(self.app.state.store.get_secret("football_data_api_key"), "test-private-key")
        status = self.client.get("/api/data-sources")
        self.assertNotIn("test-private-key", status.text)
        row = next(row for row in status.json()["sources"] if row["id"] == "football_data")
        self.assertTrue(row["configured"])
        self.assertEqual(self.client.put("/api/data-sources/unknown/credential", json={"api_key": "bad"}).status_code, 404)
        response = self.client.delete("/api/data-sources/football_data/credential")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.app.state.store.get_secret("football_data_api_key"), "")

    def test_scheduler_status_has_no_credentials(self):
        response = self.client.get("/api/collection/status")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("enabled", response.json())

    def test_cross_origin_cannot_replace_source_credentials(self):
        response = self.client.put("/api/data-sources/football_data/credential", json={"api_key": "bad"},
                                   headers={"Origin": "https://attacker.example"})
        self.assertEqual(response.status_code, 403)


class SchedulerLifecycleTests(IsolatedAppCase):
    def test_startup_and_shutdown_own_the_resident_task(self):
        from fastapi.testclient import TestClient
        from server.main import create_app
        app = create_app(self.path.parent / "lifecycle.sqlite3", collection_enabled=True)
        manager = app.state.collection_scheduler
        with TestClient(app) as client:
            self.assertTrue(client.get("/api/collection/status").json()["running"])
        self.assertTrue(manager.task.done())
