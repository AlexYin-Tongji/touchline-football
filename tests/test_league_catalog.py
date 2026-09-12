"""Complete, season-scoped memberships with real offline assets and provenance."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from server.club_library import CLUBS
from server.league_catalog import CATALOG, LEAGUES, MEMBERSHIPS, SEASONS, seed_league_catalog
from server.seed import seed
from server.storage import Store


ROOT = Path(__file__).resolve().parents[1]


class LeagueCatalogTests(unittest.TestCase):
    def test_all_five_leagues_are_complete_in_one_explicit_season(self):
        expected = {"PL": 20, "PD": 20, "SA": 20, "BL1": 18, "FL1": 18}
        self.assertEqual({row["id"]: row["expected_team_count"] for row in LEAGUES}, expected)
        self.assertEqual(Counter(row["league_id"] for row in MEMBERSHIPS), expected)
        self.assertEqual(len({row["id"] for row in MEMBERSHIPS}), 96)
        self.assertEqual(len({row["team_id"] for row in MEMBERSHIPS}), 96)
        self.assertEqual(len({row["provider_team_id"] for row in MEMBERSHIPS}), 96)
        self.assertEqual(len(SEASONS), 5)
        by_season = {row["id"]: row for row in SEASONS}
        clubs = {row["id"]: row for row in CLUBS}
        for member in MEMBERSHIPS:
            season = by_season[member["season_id"]]
            self.assertEqual(member["league_id"], season["league_id"])
            self.assertEqual(member["season_start_year"], 2026)
            self.assertEqual(season["season_start_year"], 2026)
            self.assertEqual(season["label"], "2026/27")
            self.assertEqual(season["provider_season"], 2026)
            self.assertTrue(member["provider_team_id"].isdigit())
            self.assertIn(member["team_id"], clubs)
            self.assertIn("2026", season["provider_season_start"])
            self.assertIn("2027", season["provider_season_end"])
            self.assertTrue(season["source_urls"] and season["reviewed_at"])
            self.assertNotIn("season_start_year", clubs[member["team_id"]])
            self.assertNotIn("league_id", clubs[member["team_id"]])

    def test_current_promotions_are_not_a_mix_of_recent_seasons(self):
        teams = {lid: {m["team_id"] for m in MEMBERSHIPS if m["league_id"] == lid} for lid in
                 ("PL", "PD", "SA", "BL1", "FL1")}
        self.assertTrue({"coventry-city", "hull-city", "ipswich-town"} <= teams["PL"])
        self.assertFalse({"burnley", "west-ham-united", "wolverhampton-wanderers"} & teams["PL"])
        self.assertTrue({"racing-santander", "deportivo-la-coruna", "malaga"} <= teams["PD"])
        self.assertTrue({"venezia", "frosinone", "monza"} <= teams["SA"])
        self.assertTrue({"schalke-04", "paderborn", "elversberg"} <= teams["BL1"])
        self.assertTrue({"troyes", "le-mans"} <= teams["FL1"])
        self.assertNotEqual(next(c for c in CLUBS if c["id"] == "paris-fc")["crest_url"],
                            next(c for c in CLUBS if c["id"] == "paris-saint-germain")["crest_url"])

    def test_league_assets_are_local_verified_images(self):
        for league in LEAGUES:
            credit = next(row for row in CATALOG["asset_attribution"] if row["league_id"] == league["id"])
            body = (ROOT / "public" / league["logo_url"].lstrip("/")).read_bytes()
            self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(hashlib.sha256(body).hexdigest(), credit["sha256"])
            self.assertEqual(len(body), credit["bytes"])
            self.assertTrue(credit["source_url"].startswith("https://"))

    def test_upgrade_preserves_history_user_clubs_and_custom_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "season-upgrade.sqlite3"
            store = Store(path)
            custom = {"id": "neighbourhood-fc", "name": "邻里足球队", "description": "个人资料"}
            old = {"id": "PL-2025:burnley", "league_id": "PL", "season_id": "PL-2025",
                   "season_start_year": 2025, "team_id": "burnley", "provider_team_id": "379"}
            store.put("teams", custom)
            store.put("teams", {"id": "burnley", "name": "伯恩利"})
            store.put("leagues", {"id": "PL", "name": "我关注的英超", "note": "保留"})
            store.put("seasons", {"id": "PL-2025", "league_id": "PL", "season_start_year": 2025})
            store.put("league_memberships", old)
            store.save_settings({"followed_teams": ["burnley", "neighbourhood-fc"]})
            seed(store)
            first = {collection: store.list(collection) for collection in
                     ("teams", "leagues", "seasons", "league_memberships")}
            seed_league_catalog(store)
            seed(store)
            self.assertEqual(first, {collection: store.list(collection) for collection in first})
            self.assertEqual(store.get("teams", custom["id"]), custom)
            self.assertEqual(store.get("league_memberships", old["id"]), old)
            self.assertEqual(store.get("leagues", "PL")["name"], "我关注的英超")
            self.assertEqual(store.settings()["followed_teams"], ["burnley", "neighbourhood-fc"])
            self.assertEqual(len(store.list("league_memberships")), 97)
            store.close()
            reopened = Store(path)
            self.assertEqual(reopened.get("league_memberships", old["id"]), old)
            self.assertEqual(len(reopened.list("league_memberships")), 97)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
