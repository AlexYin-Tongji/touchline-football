"""Cold clubs use the same bounded data path without authorizing model work."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from server.club_preparation import ClubPreparationManager, RECENT_TEAMS_KEY, recent_teams
from server.collection_scheduler import CollectionScheduler
from server.storage import Store
from tests.support import IsolatedAppCase


class PreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "cold.sqlite3")
        self.addCleanup(self.store.close)
        for team in ("visited", "followed", "expired"):
            self.store.put("teams", {"id": team})
        self.store.save_settings({"followed_teams": ["followed"]})
        self.store.put("evidence", {"id": "article", "team_id": "visited", "mode": "live", "origin": "manual",
                                   "text": "Explicitly supplied test material", "spoiler": False})
        self.collect = AsyncMock(return_value={"evidence_ids": ["article"], "candidates": [{"id": "candidate"}],
                                              "errors": [], "source_errors": [], "attempted_reads": 1, "added": 1})
        self.maintenance = unittest.mock.Mock()
        self.manager = ClubPreparationManager(self.store, self.maintenance, news_collector=self.collect)

    async def asyncTearDown(self):
        await self.manager.close()

    async def finish(self, kind="news", team="visited"):
        await self.manager.tasks[(kind, team)]
        return self.manager.status(kind, team)

    async def test_unfollowed_team_is_prepared_without_model_or_follow_change(self):
        with patch("server.news_reports.enqueue_reports") as queue, patch("server.provider.request_completion") as model:
            first = self.manager.start("news", "visited")
            self.assertTrue(first["started"])
            self.assertFalse(self.manager.start("news", "visited")["started"])
            result = await self.finish()
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["counts"]["readable"], 1)
            self.assertEqual(self.store.settings()["followed_teams"], ["followed"])
            queue.assert_not_called()
            model.assert_not_called()
        self.assertFalse(self.manager.start("news", "visited")["started"])
        self.collect.assert_awaited_once()

    async def test_empty_and_failed_discovery_are_not_success(self):
        self.collect.return_value = {"evidence_ids": [], "errors": []}
        self.manager.start("news", "visited")
        self.assertEqual((await self.finish())["status"], "empty")
        self.collect.return_value = {"evidence_ids": [], "errors": ["timeout"], "source_errors": []}
        self.manager.start("news", "expired")
        failed = await self.finish(team="expired")
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(self.manager.start("news", "expired")["started"])

    async def test_data_failure_is_safe_and_restart_is_retryable(self):
        self.collect.side_effect = RuntimeError("secret must not appear")
        self.manager.start("news", "visited")
        result = await self.finish()
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("secret", str(result))
        self.store.set_meta(self.manager._key("news", "visited"), {"status": "running"})
        self.assertEqual(self.manager.status("news", "visited")["status"], "interrupted")
        self.assertTrue(self.manager.start("news", "visited")["started"])
        await self.finish()

    async def test_stop_propagates_cancellation_and_keeps_existing_material(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        async def collect(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        self.manager.news_collector = collect
        self.store.put("evidence", {"id": "old", "team_id": "visited", "title": "user note"})
        self.manager.start("news", "visited")
        await entered.wait()
        await self.manager.stop("news", "visited")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.manager.status("news", "visited")["status"], "interrupted")
        self.assertEqual(self.store.get("evidence", "old")["title"], "user note")

    async def test_active_editorial_work_is_not_invalidated_by_background_reading(self):
        self.store.set_meta("workflow:news:visited", {"status": "running"})
        self.assertFalse(self.manager.start("news", "visited")["started"])
        self.collect.assert_not_awaited()
        self.assertEqual(self.store.get("evidence", "article")["text"], "Explicitly supplied test material")

    async def test_retracted_source_is_not_promised_or_exposed_in_preparation(self):
        self.store.put("sources", {"id": "removed", "team_id": "visited", "mode": "live", "enabled": False,
                                   "url": "https://example.com/removed"})
        self.store.put("evidence", {"id": "article", "team_id": "visited", "mode": "live", "origin": "web",
                                   "source_id": "removed", "private": True, "url": "https://example.com/secret", "text": "secret"})
        self.store.set_meta(self.manager._key("news", "visited"), {"status": "ready", "counts": {"readable": 1},
                            "evidence_ids": ["article"], "sources": [{"id": "removed", "url": "https://example.com/secret"}],
                            "source_errors": [{"source_id": "removed", "message": "secret"}]})
        result = self.manager.status("news", "visited")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["counts"]["readable"], 0)
        self.assertNotIn("secret", str(result))

    async def test_recent_clubs_enter_fair_upkeep_and_expire(self):
        moment = datetime.now(timezone.utc)
        self.store.set_meta(RECENT_TEAMS_KEY, {"visited": moment.isoformat(), "expired": (moment - timedelta(days=8)).isoformat(),
                                              "unknown": moment.isoformat()})
        self.assertEqual(recent_teams(self.store, moment=moment), ["visited"])
        reviewer = unittest.mock.Mock()
        reviewer.review_team = AsyncMock(return_value={"started": False, "status": "fresh"})
        scheduler = CollectionScheduler(self.store, reviewer)
        await scheduler.run_once(now=moment)
        self.assertEqual([call.args[0] for call in reviewer.review_team.await_args_list], ["followed", "visited"])

    async def test_queue_capacity_and_close_are_bounded(self):
        self.manager.MAX_PENDING = 1
        self.manager.start("news", "visited")
        blocked = self.manager.start("news", "expired")
        self.assertFalse(blocked["started"])
        self.assertEqual(blocked["status"], "idle")
        await self.manager.close()
        self.assertFalse(self.manager.tasks)
        self.assertEqual(self.manager.status("news", "visited")["status"], "interrupted")


class PreparationApiTests(IsolatedAppCase):
    def test_reading_a_cold_club_is_read_only_and_explicit_prepare_is_available(self):
        manager = self.app.state.preparation
        with patch.object(manager, "start", return_value={"started": True, "status": "running"}) as start:
            self.assertEqual(self.client.get("/api/teams/getafe/center").status_code, 200)
            self.assertEqual(self.client.get("/api/feed?team_id=getafe").status_code, 200)
            self.assertEqual(self.client.get("/api/teams/getafe/preparation?kind=news").json()["status"], "idle")
            start.assert_not_called()
            self.assertEqual(self.client.post("/api/teams/getafe/prepare", json={"kind": "team"}).status_code, 202)
            start.assert_called_once_with("team", "getafe")
        self.assertEqual(self.client.post("/api/teams/unknown/prepare", json={"kind": "team"}).status_code, 404)
        self.assertEqual(self.client.post("/api/teams/getafe/prepare", json={"kind": "model"}).status_code, 422)

    def test_cross_origin_cannot_trigger_preparation(self):
        response = self.client.post("/api/teams/getafe/prepare", json={"kind": "news"}, headers={"Origin": "https://attacker.example"})
        self.assertEqual(response.status_code, 403)
