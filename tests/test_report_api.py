import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from server.main import create_app


class ReportAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.tmp.name) / "data.db")
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_import_queues_report_without_model_and_settings_can_pause(self):
        article = {"team_id": "arsenal", "title": "训练安排的后续观察", "text": "这是接口验证材料，记录一次训练计划与参加训练的范围。材料没有宣布下一场的正式名单，仍需等待正式发布。"}
        self.assertEqual(self.client.post("/api/import", json=article).status_code, 200)
        reports = self.client.get("/api/reports?mode=live&team_id=arsenal").json()["reports"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["status"], "needs_configuration")
        self.assertEqual(self.app.state.store.list("runs", "live"), [])
        self.client.put("/api/settings", json={"reports": {"auto_generate": False}})
        self.assertEqual(self.client.post("/api/import", json={**article, "title": "另一条训练计划", "text": article["text"] + "这是另一个独立版本。"}).status_code, 200)
        self.assertEqual(len(self.client.get("/api/reports?mode=live").json()["reports"]), 1)

    def test_private_report_runs_cannot_bypass_staleness_through_old_run_routes(self):
        run = {"id": "run_old_report", "mode": "live", "team_id": "arsenal", "skill": "news_report", "internal_report": True,
               "report_id": "report_deleted", "conversation_id": "conversation_report", "status": "completed", "answer": "private stale claim",
               "structured_answer": {"summary": "private stale claim"}, "verification": {"claims": [{"text": "private stale claim"}]},
               "evidence_ids": [], "steps": [{"detail": "private stale claim"}]}
        self.app.state.store.put("runs", run)
        self.assertNotIn(run["id"], str(self.client.get("/api/state?mode=live").json()["runs"]))
        response = self.client.get("/api/runs/" + run["id"])
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("private stale claim", response.text)
        self.assertEqual(response.json()["evidence_ids"], [])
        self.assertEqual(self.client.post("/api/runs/" + run["id"] + "/resume").status_code, 409)
        self.assertEqual(self.client.post("/api/runs/" + run["id"] + "/cancel").status_code, 409)

    def test_report_endpoint_honors_team_filter_and_never_returns_demo_as_live(self):
        self.assertEqual(self.client.get("/api/reports?team_id=missing").status_code, 404)
        self.assertEqual(self.client.get("/api/reports?mode=demo").json()["reports"], [])
        self.assertEqual(self.client.get("/api/reports/report_missing").status_code, 404)
        self.assertEqual(self.client.post("/api/reports/report_missing/retry").status_code, 404)


if __name__ == "__main__":
    unittest.main()
