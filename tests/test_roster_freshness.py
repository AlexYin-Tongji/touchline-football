"""A squad list is rechecked on the clock its own situation deserves.

A single daily constant cannot express this: a list that is accurate on a
Tuesday in October can be three players out of date on deadline day, and a list
is at its least reliable exactly when the next fixture is nearest. The earlier
behaviour re-read every squad once a day and told the reader only "updated N
hours ago", which does not say whether that is recent or overdue.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from server import provenance, team_center
from server.storage import Store

TEAM = {"id": "arsenal", "name": "阿森纳", "english_name": "Arsenal"}
LEAGUE = {"id": "PL", "name": "英超", "provider_code": "eng.1", "expected_team_count": 20}
SEASON = {"id": "PL-2026", "league_id": "PL", "season_start_year": 2026, "label": "2026/27"}


def moment(**offset):
    return (datetime.now(timezone.utc) + timedelta(**offset)).isoformat()


class RosterClockTests(unittest.TestCase):
    def setUp(self):
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

    def add_fixture(self, kickoff, *, status="scheduled", match_id="m1"):
        self.store.put("league_matches", {"id": match_id, "season_id": "PL-2026",
                                          "home_team_id": "arsenal", "away_team_id": "other-0",
                                          "kickoff": kickoff, "status": status})

    def test_mid_season_a_squad_is_rechecked_daily(self):
        ttl = team_center.roster_ttl(self.store, "arsenal", "PL-2026", now="2026-11-01T00:00:00+00:00")
        self.assertEqual(ttl, 86400)

    def test_the_transfer_window_shortens_it(self):
        ttl = team_center.roster_ttl(self.store, "arsenal", "PL-2026", now="2026-08-10T00:00:00+00:00")
        self.assertEqual(ttl, provenance.TRANSFER_WINDOW_TTL)
        self.assertLess(ttl, 86400)

    def test_a_fixture_within_two_days_shortens_it_further(self):
        self.add_fixture(moment(hours=30))
        ttl = team_center.roster_ttl(self.store, "arsenal", "PL-2026")
        self.assertEqual(ttl, provenance.MATCH_DAY_TTL)

    def test_a_distant_fixture_leaves_the_season_clock_alone(self):
        self.add_fixture(moment(days=20))
        ttl = team_center.roster_ttl(self.store, "arsenal", "PL-2026")
        self.assertNotEqual(ttl, provenance.MATCH_DAY_TTL)

    def test_a_finished_fixture_does_not_accelerate_anything(self):
        self.add_fixture(moment(hours=-3), status="finished")
        ttl = team_center.roster_ttl(self.store, "arsenal", "PL-2026")
        self.assertNotEqual(ttl, provenance.MATCH_DAY_TTL)

    def test_the_nearest_fixture_is_the_one_that_counts(self):
        self.add_fixture(moment(days=30), match_id="far")
        self.add_fixture(moment(hours=3), match_id="near")
        self.assertEqual(team_center.roster_ttl(self.store, "arsenal", "PL-2026"), provenance.MATCH_DAY_TTL)

    def test_another_teams_fixture_is_not_ours(self):
        self.store.put("league_matches", {"id": "x", "season_id": "PL-2026", "home_team_id": "other-1",
                                          "away_team_id": "other-2", "kickoff": moment(hours=2),
                                          "status": "scheduled"})
        self.assertNotEqual(team_center.roster_ttl(self.store, "arsenal", "PL-2026"), provenance.MATCH_DAY_TTL)

    def test_another_seasons_fixture_is_not_ours(self):
        self.store.put("league_matches", {"id": "y", "season_id": "PL-2025", "home_team_id": "arsenal",
                                          "away_team_id": "other-0", "kickoff": moment(hours=2),
                                          "status": "scheduled"})
        self.assertNotEqual(team_center.roster_ttl(self.store, "arsenal", "PL-2026"), provenance.MATCH_DAY_TTL)


class ReaderNoteTests(unittest.TestCase):
    """The interface has to say why a value is or is not still current."""

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

    def test_a_match_week_is_explained(self):
        self.store.put("league_matches", {"id": "m", "season_id": "PL-2026", "home_team_id": "arsenal",
                                          "away_team_id": "other", "kickoff": moment(hours=5),
                                          "status": "scheduled"})
        note = team_center.transfer_window_note(self.store, "arsenal", "PL-2026")
        self.assertIn("比赛临近", note)

    def test_a_window_month_is_explained(self):
        note = team_center.transfer_window_note(self.store, "arsenal", "PL-2026",
                                                now=datetime(2026, 8, 15, tzinfo=timezone.utc))
        self.assertIn("转会窗", note)

    def test_an_ordinary_week_is_explained(self):
        note = team_center.transfer_window_note(self.store, "arsenal", "PL-2026",
                                                now=datetime(2026, 11, 15, tzinfo=timezone.utc))
        self.assertIn("赛季期间", note)


class StaleFlagTests(unittest.TestCase):
    """A squad past its own budget is reported stale, not silently current."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "team.sqlite3")
        self.addCleanup(self.store.close)

    def test_a_snapshot_past_its_window_is_stale(self):
        # Fourteen hours old is fresh on the season clock and stale on the
        # twelve-hour window clock. The snapshot is identical; the situation is
        # not, which is the whole reason this was changed.
        state = team_center._component_status(moment(hours=-14), provenance.TRANSFER_WINDOW_TTL, True)
        self.assertTrue(state["stale"])

    def test_the_same_snapshot_is_fresh_on_the_season_clock(self):
        state = team_center._component_status(moment(hours=-14), 86400, True)
        self.assertFalse(state["stale"])


if __name__ == "__main__":
    unittest.main()
