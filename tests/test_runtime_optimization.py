"""Cross-boundary regressions for batching, lightweight reads and agent progress."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from server import agent, ingestion
from server.agent_tools import LocalTools
from server.main import create_app
from server.storage import Store
from tests.test_agent import FakeStore, tool_message


class StorageBatchTests(unittest.TestCase):
    def test_previously_examined_stale_report_does_not_repeat_full_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            store = Store(path)
            store.put("reports", {"id": "old", "status": "stale"})
            store.close()
            for _ in range(2):
                store = Store(path)
                self.assertEqual(store.get("reports", "old")["status"], "stale")
                store.close()
            self.assertEqual(len(list((path.parent / "backups").glob("*.sqlite3"))), 1)

    def test_outer_failure_rolls_back_nested_writes_and_body_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            try:
                with self.assertRaisesRegex(RuntimeError, "interrupt"):
                    with store.atomic():
                        store.put("teams", {"id": "new-club"})
                        with store.atomic():
                            store.set_meta("new-marker", True)
                            store.put_many("events", [{"id": "new-event"}])
                        store.put("evidence", {"id": "article", "mode": "live", "origin": "web",
                                  "url": "https://example.com/story", "text": "temporary publisher body"})
                        raise RuntimeError("interrupt")
                self.assertIsNone(store.get("teams", "new-club"))
                self.assertIsNone(store.get("events", "new-event"))
                self.assertIsNone(store.get_meta("new-marker"))
                self.assertFalse(store._bodies)
            finally:
                store.close()

    def test_upgrade_failure_preserves_old_record_and_matching_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite3"
            store = Store(path)
            store._db.execute("DELETE FROM metadata WHERE key='transient_bodies_v1'")
            original = {"id": "legacy", "mode": "live", "team_id": "arsenal", "origin": "web",
                        "url": "https://example.com/story", "text": "old body that must survive failed migration"}
            store._db.execute("INSERT INTO documents VALUES('evidence','legacy',?)", (json.dumps(original),))
            store._db.commit()
            store.close()
            key = path.with_suffix(".key").read_bytes()
            with patch("server.news_reports.migrate_report_boundaries", side_effect=RuntimeError("migration stopped")):
                with self.assertRaisesRegex(RuntimeError, "migration stopped"):
                    Store(path)
            with closing(sqlite3.connect(path)) as db:
                persisted = json.loads(db.execute("SELECT body FROM documents WHERE id='legacy'").fetchone()[0])
            self.assertEqual(persisted["text"], original["text"])
            backups = list((path.parent / "backups").glob("*.sqlite3"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].with_suffix(".key").read_bytes(), key)
            with closing(sqlite3.connect(backups[0])) as db:
                saved = json.loads(db.execute("SELECT body FROM documents WHERE id='legacy'").fetchone()[0])
            self.assertEqual(saved["text"], original["text"])


class LightweightAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_keeps_app_context_without_loading_history(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory) / "api.sqlite3")
            store = app.state.store
            original_list = store.list
            def guarded(collection, *args, **kwargs):
                if collection in {"evidence", "events", "runs", "reports", "tracking", "recommendations"}:
                    raise AssertionError("summary loaded historical " + collection)
                return original_list(collection, *args, **kwargs)
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                    with patch.object(store, "list", side_effect=guarded):
                        response = await client.get("/api/state?mode=live&team_id=arsenal&view=summary")
                    self.assertEqual(response.status_code, 200)
                    data = response.json()
                    self.assertTrue(data["teams"])
                    self.assertIn("model_roles_configured", data["setup"])
                    self.assertEqual(data["runs"], [])
                    self.assertEqual(data["evidence"], [])
            finally:
                store.close()


class AgentProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_alternating_unchanged_tools_stop_before_long_task_timeout(self):
        store = FakeStore()
        store.new_run()
        messages = [tool_message("team_context" if i % 2 == 0 else "reader_profile", {}) for i in range(20)]
        requester = AsyncMock(side_effect=[*messages, agent.AgentFailure("test-only ceiling")])
        with patch("server.agent.request_completion", requester):
            await agent.run_agent(store, "run_one", asyncio.Event())
        result = store.get("runs", "run_one")
        self.assertEqual(result["status"], "failed")
        self.assertIn("重复", result["error"])
        self.assertLess(requester.call_count, 12)

    async def test_one_unavailable_selected_source_keeps_readable_source(self):
        store = FakeStore()
        store.new_run(input_evidence_ids=["ev_injury", "ev_transfer"])
        original = LocalTools.acall
        async def read(local, name, arguments):
            if name == "read_evidence" and arguments.get("evidence_id") == "ev_transfer":
                raise ingestion.IngestionError("source temporarily unavailable")
            return await original(local, name, arguments)
        requester = AsyncMock(return_value={"role": "assistant", "content": "中场球员仍未恢复训练。[ev_injury]"})
        with patch.object(LocalTools, "acall", read), patch("server.agent.request_completion", requester):
            await agent.run_agent(store, "run_one", asyncio.Event())
        result = store.get("runs", "run_one")
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["evidence_ids"], ["ev_injury"])
        self.assertTrue(result["warnings"])
        self.assertIn("ev_transfer", {row["evidence_id"] for row in result["source_errors"]})
