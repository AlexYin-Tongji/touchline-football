"""An emptied store plans a full refresh; a fresh one plans nothing."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from server import collection, team_center
from server.storage import Store
from server.ingestion import IngestionError

TEAM = {"id": "arsenal", "name": "阿森纳", "english_name": "Arsenal"}
LEAGUE = {"id": "PL", "name": "英超", "provider_code": "eng.1", "expected_team_count": 20}
SEASON = {"id": "PL-2026", "league_id": "PL", "season_start_year": 2026, "label": "2026/27"}


def stamp(**offset):
    return (datetime.now(timezone.utc) - timedelta(**offset)).isoformat()


class ReviewTeamTests(unittest.IsolatedAsyncioTestCase):
    """The planner decides, and this is what makes it act."""

    def setUp(self):
        offline = patch("server.league_data.fetch_json", new=AsyncMock(side_effect=IngestionError("本地测试不读取真实网络。")))
        offline.start()
        self.addCleanup(offline.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "team.sqlite3")
        self.addCleanup(self.store.close)
        self.store.put("teams", TEAM)
        self.store.put("leagues", LEAGUE)
        self.store.put("seasons", SEASON)
        self.store.put("league_memberships", {"id": "PL-2026-arsenal", "season_id": "PL-2026",
                                              "league_id": "PL", "season_start_year": 2026,
                                              "team_id": "arsenal", "provider_team_id": "359"})
        for index in range(19):
            self.store.put("league_memberships", {"id": f"other-{index}", "season_id": "PL-2026",
                                                  "league_id": "PL", "season_start_year": 2026,
                                                  "team_id": f"other-{index}",
                                                  "provider_team_id": str(4000 + index)})
        self.manager = team_center.TeamMaintenanceManager(self.store)

    async def asyncTearDown(self):
        await self.manager.close()

    async def test_an_empty_store_plans_every_core_part(self):
        result = await self.manager.review_team("arsenal")
        fields = {need["field"] for need in result["plan"]["needs"]}
        self.assertTrue({"roster", "schedule", "standings"} <= fields)
        self.assertTrue(result["started"])

    async def test_the_reason_is_a_sentence_not_a_dump(self):
        result = await self.manager.review_team("arsenal")
        self.assertIsInstance(result["reason"], str)
        self.assertTrue(result["reason"])
        self.assertNotIn("inf", result["reason"])

    async def test_the_plan_is_returned_even_when_nothing_starts(self):
        result = await self.manager.review_team("arsenal")
        self.assertIn("plan", result)
        self.assertIn("reason", result)

    async def test_only_the_selected_parts_are_requested(self):
        # A fresh standings snapshot must not be re-fetched just because the
        # squad list has never been read.
        self.store.put("standings", {"id": "PL-2026", "fetched_at": stamp(),
                                     "rows": [{"team_id": "arsenal", "rank": 1, "played": 10, "points": 24}]})
        self.store.set_meta("league_sync_PL-2026", {"standings_at": stamp(), "status": "completed"})
        result = await self.manager.review_team("arsenal", budget=5)
        planned = {need["field"] for need in result["plan"]["needs"]}
        self.assertNotIn("standings", planned)

    async def test_a_planned_portrait_pass_is_not_the_default(self):
        # Portraits are the slowest work in the tool set, so they are requested
        # only when a portrait need is what actually ranked.
        self.store.put("league_matches", {"id": "m", "season_id": "PL-2026", "home_team_id": "arsenal",
                                          "away_team_id": "other", "status": "scheduled",
                                          "kickoff": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()})
        result = await self.manager.review_team("arsenal", budget=0)
        self.assertFalse(result["started"])


class ViewClaimTests(unittest.TestCase):
    """Each part is claimed with its own clock, because they age separately."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "team.sqlite3")
        self.addCleanup(self.store.close)
        self.store.put("teams", TEAM)
        self.store.put("leagues", LEAGUE)
        self.store.put("seasons", SEASON)
        self.store.put("league_memberships", {"id": "a", "season_id": "PL-2026", "league_id": "PL",
                                              "season_start_year": 2026, "team_id": "arsenal",
                                              "provider_team_id": "359"})

    def claims(self, view):
        return team_center.team_view_claims(self.store, "arsenal", view, year=2026)

    def test_every_core_part_produces_a_claim(self):
        claims = self.claims({"components": {}, "roster": [], "standings": [], "matches": []})
        self.assertTrue({"roster", "schedule", "standings", "portraits"} <= set(claims))

    def test_a_part_never_fetched_has_no_clock(self):
        claims = self.claims({"components": {}, "roster": [], "standings": [], "matches": []})
        self.assertEqual(claims["roster"]["claims"]["roster"].fetched_at, "")

    def test_the_most_overdue_fetched_part_ranks_first(self):
        # The schedule has never been read at all, which ranks it above anything
        # merely late; among the fetched parts, the six-week-old table is the
        # one that most needs re-checking.
        view = {"components": {"roster": {"updated_at": stamp(hours=2)},
                               "schedule": {"updated_at": stamp(hours=2)},
                               "standings": {"updated_at": stamp(days=40)}},
                "roster_updated_at": stamp(hours=2), "updated_at": stamp(days=40),
                "roster": [{"id": "p1"}], "standings": [{"team_id": "arsenal"}], "matches": []}
        needs = collection.needs_from(self.claims(view))
        fetched = [need.field for need in needs if need.overdue_ratio != float("inf")]
        self.assertEqual(fetched[0], "standings")

    def test_a_missing_part_says_so_in_its_own_note(self):
        claims = self.claims({"components": {}, "roster": [], "standings": [], "matches": []})
        self.assertIn("还没有", claims["roster"]["claims"]["roster"].note)

    def test_pending_photos_are_scheduled_as_coverage_work_instead_of_year_fresh_facts(self):
        view = {"components": {"portraits": {"status": "partial", "last_attempt": stamp(minutes=2),
                                             "retry_seconds": 60, "community_remaining_count": 16}},
                "roster": [{"id": "p1", "photo_url": "https://a.espncdn.com/i/headshots/soccer/players/full/1.png"},
                           {"id": "p2", "photo_url": ""}], "standings": [], "matches": []}
        claims = self.claims(view)
        claim = claims["portraits"]["claims"]["photo_url"]
        self.assertEqual(claim.tier.value, "derived")
        self.assertEqual(claim.value, 1)
        self.assertTrue(any(need.field == "photo_url" for need in collection.needs_from(claims)))

    def test_an_untouched_optional_part_does_not_outrank_real_staleness(self):
        # "We have not tried the photo pass yet" is not more urgent than a
        # table that is genuinely six weeks old.
        view = {"components": {"schedule": {"updated_at": stamp(hours=2)},
                               "standings": {"updated_at": stamp(days=40)}},
                "schedule_updated_at": stamp(hours=2), "updated_at": stamp(days=40),
                "roster": [{"id": "p1"}], "standings": [{"team_id": "arsenal"}], "matches": []}
        needs = collection.needs_from(self.claims(view))
        fetched = [need.field for need in needs if need.overdue_ratio != float("inf")]
        self.assertEqual(fetched[0], "standings")

    def test_the_squad_claim_takes_the_fixture_clock(self):
        # A squad list tightens before kickoff; standings do not.
        self.store.put("league_matches", {"id": "m", "season_id": "PL-2026", "home_team_id": "arsenal",
                                          "away_team_id": "other", "status": "scheduled",
                                          "kickoff": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()})
        claims = self.claims({"components": {}, "season": {"id": "PL-2026"}, "roster": [],
                              "standings": [], "matches": []})
        self.assertLessEqual(claims["roster"]["claims"]["roster"].ttl_seconds, 2 * 3600)


if __name__ == "__main__":
    unittest.main()
