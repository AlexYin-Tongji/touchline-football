"""ESPN speaks as a dataset source, and its squad claims carry freshness."""
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path

from server import provenance as prov
from server.providers import Capability, SourceTier, sources
from server.providers import espn as espn_module
from server.providers.espn import EspnProvider
from server.storage import Store


def snapshot(team_id="arsenal", season_id="premier-league-2026", fetched=None, players=2):
    roster = []
    for index in range(players):
        roster.append({
            "id": f"espn-player-{1000 + index}", "provider_id": str(1000 + index),
            "name": f"Player {index}", "team_id": team_id, "season_id": season_id,
            "number": str(index + 1), "position": "中场", "position_group": "MF",
            "nationality": "英格兰", "birth_date": "2000-01-01", "age": 26,
            "height_cm": 180, "weight_kg": 75.0, "stats": {},
        })
    return {"id": f"squad-{season_id}-{team_id}", "team_id": team_id, "season_id": season_id,
            "roster": roster, "fetched_at": fetched or datetime.now(timezone.utc).isoformat()}


class EspnRegistrationTests(unittest.TestCase):
    def test_it_speaks_as_a_dataset_source(self):
        row = next(item for item in sources() if item.id == "espn")
        self.assertEqual(row.tier, SourceTier.DATASET)

    def test_its_hosts_carry_the_registered_tier(self):
        self.assertEqual(prov.tier_for_url("https://www.espn.com/soccer/team/_/id/359"), SourceTier.DATASET)
        self.assertEqual(prov.tier_for_url("https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1"),
                         SourceTier.DATASET)

    def test_it_declares_the_capabilities_the_tools_need(self):
        row = next(item for item in sources() if item.id == "espn")
        self.assertTrue({Capability.SQUAD, Capability.FIXTURE, Capability.STANDINGS} <= row.capabilities)


class SquadClaimTests(unittest.TestCase):
    """A stored snapshot becomes field-level claims, not one opaque blob."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "app.sqlite3")
        self.addCleanup(self.store.close)
        self.store.put("teams", {"id": "arsenal", "name": "阿森纳", "english_name": "Arsenal"})
        self.store.put("leagues", {"id": "premier-league", "name": "英超", "provider_code": "eng.1",
                                   "expected_team_count": 20})
        self.store.put("seasons", {"id": "premier-league-2026", "league_id": "premier-league",
                                   "season_start_year": 2026, "label": "2026/27"})
        members = {"team_id": "arsenal", "season_id": "premier-league-2026", "league_id": "premier-league",
                   "season_start_year": 2026, "provider_team_id": "359"}
        self.store.put("league_memberships", {"id": "arsenal-2026", **members})
        for index in range(19):
            self.store.put("league_memberships", {"id": f"other-{index}", "team_id": f"other-{index}",
                                                  "season_id": "premier-league-2026",
                                                  "league_id": "premier-league", "season_start_year": 2026,
                                                  "provider_team_id": str(4000 + index)})
        self.provider = EspnProvider(store=self.store)

    def _store_snapshot(self, **kwargs):
        item = snapshot(**kwargs)
        self.store.put("team_snapshots", item)
        return item

    def test_a_stored_squad_becomes_one_claim_per_field(self):
        self._store_snapshot()
        answer = self.provider.squad("arsenal")
        self.assertTrue(answer.ok)
        self.assertEqual(len(answer.items), 2)
        claims = answer.items[0]["claims"]
        self.assertEqual(claims["name"].value, "Player 0")
        self.assertEqual(claims["height_cm"].value, 180)
        self.assertEqual(claims["birth_date"].value, "2000-01-01")

    def test_reading_the_squad_never_makes_a_request(self):
        self._store_snapshot()
        answer = self.provider.squad("arsenal")
        self.assertTrue(answer.cached)
        self.assertEqual(answer.spent.value, "light")

    def test_a_missing_snapshot_is_a_gap_not_an_empty_squad(self):
        answer = self.provider.squad("arsenal")
        self.assertEqual(answer.items, [])
        self.assertFalse(answer.ok)
        self.assertIn("阵容快照", answer.gaps[0]["message"])

    def test_an_absent_field_is_omitted_rather_than_defaulted(self):
        item = self._store_snapshot()
        item["roster"][0]["height_cm"] = None
        self.store.put("team_snapshots", item)
        claims = self.provider.squad("arsenal").items[0]["claims"]
        self.assertNotIn("height_cm", claims)
        self.assertIn("birth_date", claims)

    def test_a_birth_date_is_not_made_urgent_by_the_season_clock(self):
        # Height, nationality and birth date do not turn over; a squad list does.
        self._store_snapshot()
        claims = self.provider.squad("arsenal").items[0]["claims"]
        self.assertEqual(claims["birth_date"].ttl_seconds, prov.YEAR)
        self.assertEqual(claims["nationality"].ttl_seconds, prov.YEAR)
        self.assertLessEqual(claims["number"].ttl_seconds, 30 * prov.DAY)

    def test_every_claim_names_espn_as_its_source(self):
        self._store_snapshot()
        for row in self.provider.squad("arsenal").items:
            for claim in row["claims"].values():
                self.assertEqual(claim.source_id, "espn")
                self.assertEqual(claim.tier, SourceTier.DATASET)


class PersonClaimTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "app.sqlite3")
        self.addCleanup(self.store.close)
        self.provider = EspnProvider(store=self.store)

    def test_a_player_identity_is_attributed(self):
        answer = self.provider.person("12345", name="Someone")
        self.assertEqual(answer.items[0]["claim"].source_id, "espn")

    def test_a_non_numeric_identifier_is_refused(self):
        self.assertFalse(self.provider.person("abc").ok)


if __name__ == "__main__":
    unittest.main()
