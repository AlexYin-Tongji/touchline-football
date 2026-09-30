"""Portrait caching must not throw away a photo it already verified.

These cases exist because a version bump and a too-short TTL together made a
verified, licence-checked portrait permanently unreachable, and because the
community fallback was skipped for exactly the players Wikimedia had once filled.
"""
import unittest

from server import wikimedia_portraits
from server.storage import Store
import tempfile
from pathlib import Path


PLAYER = {"name": "Thibaut Courtois", "birth_date": ""}
TEAM = "real-madrid"
COMPLETED = {"status": "completed", "fetched_at": "2026-09-23T07:44:21+00:00",
             "photo_url": "https://upload.wikimedia.org/x.jpg", "photo_credit": "Bryan Berlin",
             "photo_source_url": "https://en.wikipedia.org/wiki/Thibaut_Courtois",
             "photo_file_page": "https://commons.wikimedia.org/wiki/File:X.jpg",
             "photo_license": "CC BY-SA 4.0", "photo_match": "wikimedia_verified_club"}


class CacheLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "portraits.sqlite3")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_a_completed_portrait_survives_longer_than_a_day(self):
        # Held at 24h, a photo fetched five days before a roster rebuild was
        # dropped even though it was still a correct photo of the same person.
        self.store.set_meta(wikimedia_portraits._portrait_key(PLAYER, TEAM), COMPLETED)
        cached = wikimedia_portraits.cached_portrait(self.store, PLAYER, TEAM)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["status"], "completed")

    def test_the_previous_generation_is_read_as_a_fallback(self):
        # The v1 -> v2 bump changed every key, orphaning entries that were already
        # verified. A real photo became unreadable because the key addressing it
        # was no longer computed.
        self.store.set_meta(wikimedia_portraits._portrait_key(PLAYER, TEAM, generation=1), COMPLETED)
        cached = wikimedia_portraits.cached_portrait(self.store, PLAYER, TEAM)
        self.assertIsNotNone(cached, "a verified portrait was left unreachable after the key bump")
        self.assertEqual(cached["photo_license"], "CC BY-SA 4.0")

    def test_the_current_generation_wins_over_the_previous_one(self):
        self.store.set_meta(wikimedia_portraits._portrait_key(PLAYER, TEAM, generation=1),
                            dict(COMPLETED, photo_credit="older"))
        self.store.set_meta(wikimedia_portraits._portrait_key(PLAYER, TEAM),
                            dict(COMPLETED, photo_credit="newer"))
        self.assertEqual(wikimedia_portraits.cached_portrait(self.store, PLAYER, TEAM)["photo_credit"], "newer")

    def test_a_verdict_keeps_its_short_window(self):
        # A verdict is about right now, so it must still expire quickly.
        self.store.set_meta(wikimedia_portraits._portrait_key(PLAYER, TEAM),
                            {"status": "rejected", "fetched_at": "2020-01-01T00:00:00+00:00",
                             "photo_reason": "没有可用的自由许可图片。"})
        self.assertIsNone(wikimedia_portraits.cached_portrait(self.store, PLAYER, TEAM))

    def test_a_retryable_failure_is_not_read_as_a_verdict(self):
        self.store.set_meta(wikimedia_portraits._portrait_key(PLAYER, TEAM),
                            {"status": "unavailable", "last_attempt": "2020-01-01T00:00:00+00:00",
                             "photo_reason": "维基媒体查询过于频繁或暂时不可达，稍后自动重试。"})
        self.assertIsNone(wikimedia_portraits.cached_portrait(self.store, PLAYER, TEAM))

    def test_an_empty_cache_returns_nothing(self):
        self.assertIsNone(wikimedia_portraits.cached_portrait(self.store, PLAYER, TEAM))


class ReplayExplainsAnEmptyResultTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "portraits.sqlite3")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_a_player_with_no_cached_verdict_states_that_instead_of_nothing(self):
        # An empty reason reads as "this player has no photo". The truth when
        # neither cache holds a verdict is "the check has not produced one yet",
        # and the reader is owed that distinction.
        from server import team_portraits
        snapshot = {"team_id": TEAM, "season_id": "PD-2026",
                    "roster": [{"provider_id": "1", "id": "p1", **PLAYER, "photo_url": ""}]}
        result = team_portraits.apply_cached_portraits(self.store, snapshot)
        row = result["roster"][0]
        self.assertFalse(row["photo_url"])
        self.assertTrue(row.get("photo_reason"))
        self.assertTrue(row.get("retryable"))
        self.assertEqual(row.get("reason_code"), "not_yet_resolved")


if __name__ == "__main__":
    unittest.main()
