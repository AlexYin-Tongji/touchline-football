import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from server.main import create_app


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.directory.name) / "tracking.sqlite3")
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.client.put("/api/settings", json={"spoiler_free": True})

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def wait_run(self, run_id):
        for _ in range(100):
            run = self.client.get(f"/api/runs/{run_id}").json()
            if run["status"] not in {"queued", "running"}:
                return run
            time.sleep(.02)
        self.fail("Tracking check did not finish")

    def create_tracking(self, **values):
        result = self.client.post("/api/tracking", json={"team_id": "arsenal", "title": "跟进训练",
            "query": "训练", "interval_minutes": 60, **values})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_changed_then_unchanged_avoids_repeated_model_and_keeps_history(self):
        doc = self.client.post("/api/import", json={"team_id": "arsenal", "title": "训练更新",
            "text": "测试训练记录：球员恢复了部分训练，目前尚无完整的医学许可或出场名单，需要进一步确认。"}).json()
        track = self.create_tracking(event_id=doc["event"]["id"])
        collected = {"added": 0, "unchanged": 1, "evidence_ids": [doc["evidence"]["id"]], "errors": [], "event_ids": [doc["event"]["id"]]}
        async def model(store, run_id, cancel):
            run = store.get("runs", run_id)
            run.update(status="completed", answer="来源新增训练说明，尚未确认出场。", evidence_ids=[doc["evidence"]["id"]], finished_at="2026-09-09T00:00:00+00:00")
            store.put("runs", run)
        generation = AsyncMock(side_effect=model)
        with patch("server.research.collect_team", AsyncMock(return_value=collected)), patch("server.agent.run_agent", generation):
            first = self.client.post(f"/api/tracking/{track['id']}/check").json()
            self.assertEqual(self.wait_run(first["id"])["status"], "completed")
            second = self.client.post(f"/api/tracking/{track['id']}/check").json()
            second = self.wait_run(second["id"])
            self.assertEqual(second["status"], "completed")
            self.assertEqual(generation.await_count, 1)
            self.assertEqual(second["model"], "本次未调用模型")
        saved = self.app.state.store.get("tracking", track["id"])
        self.assertEqual(saved["status"], "unchanged")
        self.assertEqual(len(saved["updates"]), 1)
        self.assertEqual(saved["baseline_evidence_ids"], [doc["evidence"]["id"]])
        paused = self.client.patch(f"/api/tracking/{track['id']}", json={"enabled": False}).json()
        self.assertEqual(paused["status"], "paused")
        self.client.delete(f"/api/tracking/{track['id']}")
        self.assertIsNotNone(self.app.state.store.get("evidence", doc["evidence"]["id"]))
        self.assertIsNotNone(self.app.state.store.get("runs", first["id"]))

    def test_pause_cancels_active_collection_and_resume_preserves_task(self):
        task = self.create_tracking()
        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()
        with patch("server.research.collect_team", AsyncMock(side_effect=blocked)):
            run = self.client.post(f"/api/tracking/{task['id']}/check").json()
            self.client.patch(f"/api/tracking/{task['id']}", json={"enabled": False})
            result = self.wait_run(run["id"])
            self.assertEqual(result["status"], "cancelled")
            record = self.app.state.store.get("tracking", task["id"])
            self.assertFalse(record["enabled"])
            self.assertEqual(record["status"], "paused")
            restored = self.client.patch(f"/api/tracking/{task['id']}", json={"enabled": True}).json()
            self.assertEqual(restored["status"], "idle")

    def test_tracking_scope_and_configuration_protocol_validation(self):
        bad = self.client.post("/api/tracking", json={"team_id": "liverpool", "title": "错误范围", "query": "训练", "event_id": "event_demo_arsenal_1"})
        self.assertIn(bad.status_code, (400, 404))
        config = {"llm": {"protocol": "responses", "send_temperature": False, "max_output_tokens": 6000, "thinking_enabled": False, "max_rounds": 16}}
        self.assertEqual(self.client.put("/api/settings", json=config).status_code, 200)
        self.assertEqual(self.client.get("/api/state").json()["settings"]["llm"]["protocol"], "responses")
        self.assertEqual(self.client.put("/api/settings", json={"llm": {"protocol": "invented"}}).status_code, 422)
        self.assertEqual(self.client.post("/api/tracking", json={"team_id": "arsenal", "title": "空主题", "query": "  "}).status_code, 422)

    def test_saved_spoiler_summary_is_hidden_when_preference_changes(self):
        task = self.create_tracking()
        self.app.state.store.put("runs", {"id": "r_spoiler", "mode": "live", "team_id": "arsenal", "status": "completed", "skill": "event_watch", "spoiler": True, "evidence_ids": []})
        task.update(last_run_id="r_spoiler", summary="赛果是2-1", updates=[{"run_id": "r_spoiler", "summary": "赛果是2-1"}])
        self.app.state.store.put("tracking", task)
        result = self.client.get("/api/state?mode=live").json()["tracking"][0]
        self.assertNotIn("2-1", str(result))
        self.assertTrue(result["spoiler_hidden"])
        historical = self.client.get("/api/runs/r_spoiler").json()
        self.assertTrue(historical["spoiler_hidden"])
        self.assertEqual(historical["evidence"], [])

    def test_changing_query_starts_new_context_without_reusing_old_event(self):
        doc = self.client.post("/api/import", json={"team_id": "arsenal", "title": "训练资料",
            "text": "这是一份软件测试训练资料，记录了中场训练的内容，需要正式名单进一步确认出场，暂无独立佐证。"}).json()
        task = self.create_tracking(event_id=doc["event"]["id"])
        task.update(summary="原主题解读", updates=[{"at": "2026-09-09", "run_id": "old", "summary": "原主题解读"}])
        self.app.state.store.put("tracking", task)
        changed = self.client.patch(f"/api/tracking/{task['id']}", json={"query": "转会合同"}).json()
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(changed["updates"], [])
        self.assertIsNone(changed["event_id"])
        self.assertIn("新", changed["summary"])
        self.assertEqual(changed["archived_updates"][0]["query"], "训练")

    def test_safe_news_projection_keeps_context_without_exposing_old_result(self):
        from server.ingestion import ingest
        original = ("Arsenal will travel to Naples on Tuesday afternoon. The manager will speak to reporters after the training session and no starting lineup has been confirmed.\n\n"
                    "Arsenal won 2-1 on Sunday.\n\n"
                    "The squad will train together before departure. The club has confirmed the revised travel arrangements and will share further availability information later.")
        result = ingest(self.app.state.store, team_id="arsenal", title="Arsenal travel update", text=original,
                        origin="web", spoiler=True, source_name="Official source fixture")
        eid = result["evidence"]["id"]
        self.app.state.store.put("runs", {"id": "r_safe_news", "mode": "live", "team_id": "arsenal", "skill": "ask",
            "status": "completed", "answer": f"来源给出了修订的出行安排。[{eid}]", "evidence_ids": [eid],
            "redacted_evidence_ids": [eid], "spoiler": False})
        state = self.client.get("/api/state?mode=live").json()
        # Static club history URLs can contain year ranges such as 1902-1910.
        # Check the news surfaces carrying the protected match evidence.
        for collection in ("events", "evidence", "runs", "recommendations", "tracking", "matches"):
            self.assertNotIn("2-1", str(state[collection]))
        self.assertEqual(len(state["evidence"]), 1)
        self.assertTrue(state["evidence"][0]["spoiler_redacted"])
        self.assertIn("travel", state["events"][0]["summary"])
        response = self.client.get("/api/runs/r_safe_news").json()
        self.assertNotIn("2-1", str(response))
        self.assertIn("出行", response["answer"])
        self.assertIn("2-1", self.app.state.store.get("evidence", eid)["text"])

    def test_import_response_does_not_reveal_protected_original(self):
        result = self.client.post("/api/import", json={"team_id": "arsenal", "title": "赛后阿森纳2-1",
            "text": "这份手动赛后资料记录了阿森纳以2-1取得胜利，包含用户尚未观看比赛时希望隐藏的比分与比赛结果信息。", "spoiler": True}).json()
        self.assertNotIn("2-1", str(result))
        self.assertTrue(result["evidence"]["spoiler_hidden"])
        stored = self.app.state.store.get("evidence", result["evidence"]["id"])
        self.assertIn("2-1", stored["text"])
        self.assertIn("2-1", stored["title"])


if __name__ == "__main__":
    unittest.main()
