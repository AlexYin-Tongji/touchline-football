"""Bounded, persistent scheduling over the existing team review boundary."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from server.collection_scheduler import CollectionScheduler
from server.storage import Store


class Reviewer:
    def __init__(self):
        self.seen = []
        self.fail = set()
        self.entered = asyncio.Event()
        self.release = None

    async def review_team(self, team_id, **kwargs):
        self.seen.append(team_id)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if team_id in self.fail:
            raise RuntimeError("do not expose internal credentials")
        return {"started": False, "status": "fresh", "reason": "资料仍在时效内。"}

    async def wait_for_team(self, team_id):
        return {"status": "completed"}


class CollectionSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "scheduler.sqlite3")
        self.addCleanup(self.store.close)
        for name in ("a", "b", "c", "d", "e"):
            self.store.put("teams", {"id": name})
        self.store.save_settings({"followed_teams": ["a", "b", "c", "d", "e"]})
        self.reviewer = Reviewer()
        self.manager = CollectionScheduler(self.store, self.reviewer)
        self.moment = datetime(2026, 9, 28, 1, tzinfo=timezone.utc)

    async def asyncTearDown(self):
        await self.manager.close()

    async def test_batches_rotate_without_starving_later_followed_teams(self):
        for _ in range(3):
            await self.manager.run_once(now=self.moment)
        self.assertEqual(self.reviewer.seen, ["a", "b", "c", "d", "e"])
        await self.manager.run_once(now=self.moment + timedelta(seconds=5))
        self.assertEqual(len(self.reviewer.seen), 5)

    async def test_one_failure_does_not_block_next_team_and_has_persistent_backoff(self):
        self.reviewer.fail.add("a")
        await self.manager.run_once(now=self.moment)
        self.assertEqual(self.reviewer.seen, ["a", "b"])
        row = self.manager.status()["teams"]["a"]
        self.assertEqual(row["failures"], 1)
        self.assertNotIn("credentials", str(row))
        next_time = datetime.fromisoformat(row["next_check"])
        self.assertGreaterEqual((next_time - self.moment).total_seconds(), 300)
        replacement = CollectionScheduler(self.store, self.reviewer)
        self.assertEqual(replacement.status()["teams"]["a"]["next_check"], row["next_check"])

    async def test_overlap_is_deduplicated(self):
        self.reviewer.release = asyncio.Event()
        first = asyncio.create_task(self.manager.run_once(now=self.moment))
        await self.reviewer.entered.wait()
        second = await self.manager.run_once(now=self.moment)
        self.assertFalse(second["started"])
        self.reviewer.release.set()
        await first
        self.assertEqual(self.reviewer.seen, ["a", "b"])

    async def test_invalid_or_unfollowed_teams_never_run(self):
        self.store.save_settings({"followed_teams": ["unknown", "c", "c"]})
        await self.manager.run_once(now=self.moment)
        self.assertEqual(self.reviewer.seen, ["c"])

    async def test_manual_work_in_progress_is_not_overlapped(self):
        self.store.set_meta("workflow:team:a", {"status": "running"})
        await self.manager.run_once(now=self.moment)
        self.assertNotIn("a", self.reviewer.seen)

    async def test_close_cancels_resident_cycle_and_repeated_start_is_idempotent(self):
        self.reviewer.release = asyncio.Event()
        self.assertTrue(self.manager.start(delay=0))
        self.assertFalse(self.manager.start(delay=0))
        await asyncio.wait_for(self.reviewer.entered.wait(), 1)
        await self.manager.close()
        self.assertFalse(self.manager.status()["running"])
        self.assertEqual(len(self.reviewer.seen), 1)


if __name__ == "__main__":
    unittest.main()
