"""Catalog upgrades preserve personal records and ship usable local material."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

from server.club_library import CLUBS, RESEARCH_CATALOG, TEAM_ALIASES, TRUSTED_SOURCE_HOSTS
from server.seed import seed
from server.storage import Store


ROOT = Path(__file__).resolve().parents[1]


class ClubLibraryTests(unittest.TestCase):
    def test_every_browse_club_has_background_local_crest_and_source_provenance(self):
        self.assertGreaterEqual(len(CLUBS), 96)
        ids = {club["id"] for club in CLUBS}
        self.assertEqual(len(ids), len(CLUBS))
        self.assertTrue({"real-madrid", "bayern-munich", "paris-saint-germain", "juventus",
                         "ac-milan", "atletico-madrid", "borussia-dortmund", "roma"} <= ids)
        manifest = json.loads((ROOT / "public/assets/clubs/attribution.json").read_text(encoding="utf-8"))
        credits = {row["id"]: row for row in manifest["clubs"]}
        for club in CLUBS:
            with self.subTest(club=club["id"]):
                self.assertEqual(club["league"], club["country"])
                self.assertNotIn("league_name", club)
                self.assertGreater(len(club["description"]), 25)
                self.assertTrue(club["city"] and club["profile_sources"])
                self.assertTrue(club["official_url"].startswith("https://"))
                self.assertIn("权利归", club["crest_attribution"])
                self.assertTrue(club["crest_url"].startswith("/assets/"))
                file = ROOT / "public" / club["crest_url"].lstrip("/")
                body = file.read_bytes()
                self.assertEqual(hashlib.sha256(body).hexdigest(), credits[club["id"]]["sha256"])
                self.assertEqual(len(body), credits[club["id"]]["bytes"])
                if file.suffix == ".svg":
                    root = ElementTree.fromstring(body)
                    self.assertTrue(root.tag.endswith("svg"))
                    # Local assets must not execute script when embedded directly.
                    self.assertNotIn(b"<script", body.lower())
                elif file.suffix == ".png":
                    self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")
                    self.assertGreater(int.from_bytes(body[16:20], "big"), 32)
                    self.assertGreater(int.from_bytes(body[20:24], "big"), 32)

    def test_all_teams_have_public_sources_and_scoped_aliases(self):
        self.assertEqual(set(RESEARCH_CATALOG), {club["id"] for club in CLUBS})
        for club in CLUBS:
            sources = RESEARCH_CATALOG[club["id"]]
            with self.subTest(club=club["id"]):
                self.assertGreaterEqual(len(sources), 2)
                self.assertTrue(any(source["id"].startswith("official_") for source in sources))
                self.assertTrue(any(not source["id"].startswith("official_") for source in sources))
                self.assertTrue(any(source["kind"] == "rss" for source in sources))
                self.assertTrue(all(source["kind"] in {"rss", "html"} for source in sources))
                for source in sources:
                    if source.get("access_review", {}).get("feed_title"):
                        self.assertNotEqual(source["access_review"]["feed_title"], "Football | The Guardian")
                self.assertEqual(len({source["id"] for source in sources}), len(sources))
                for source in sources:
                    self.assertIn(urlsplit(source["url"]).hostname, TRUSTED_SOURCE_HOSTS)
                self.assertIn(club["name"], TEAM_ALIASES[club["id"]])

    def test_upgrade_preserves_custom_values_disabled_sources_and_followed_clubs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upgrade.sqlite3"
            store = Store(path)
            original = {"id": "real-madrid", "name": "我的皇马", "description": "", "city": None,
                        "crest_url": "/assets/personal.png", "profile_sources": [], "color": "#112233"}
            custom = {"id": "community", "name": "社区球队", "description": "周末一起踢球"}
            disabled = {"id": "disabled_guardian", "team_id": "real-madrid", "mode": "live",
                        "name": "已停用来源", "enabled": False,
                        "url": "https://www.theguardian.com/football/realmadrid/rss", "kind": "rss"}
            store.put("teams", original)
            store.put("teams", custom)
            store.put("sources", disabled)
            store.save_settings({"followed_teams": ["community"], "mode": "live"})
            seed(store)
            upgraded = store.get("teams", "real-madrid")
            for key, value in original.items():
                self.assertEqual(upgraded[key], value)
            self.assertEqual(upgraded["founded"], 1902)
            self.assertEqual(upgraded["short_name"], "RMA")
            self.assertEqual(store.get("teams", "community"), custom)
            self.assertEqual(store.get("sources", disabled["id"]), disabled)
            self.assertEqual(store.settings()["followed_teams"], ["community"])
            self.assertEqual(store.list("evidence", "live"), [])
            teams = store.list("teams")
            seed(store)
            self.assertEqual(store.list("teams"), teams)
            store.close()
            reopened = Store(path)
            self.assertEqual(reopened.list("teams"), teams)
            self.assertEqual(reopened.get("sources", disabled["id"]), disabled)
            reopened.close()

    def test_previously_seeded_image_urls_remain_available_without_rewriting_profiles(self):
        legacy_paths = {"real-madrid": "/assets/clubs/real-madrid.svg",
                        "paris-saint-germain": "/assets/clubs/paris-saint-germain.avif"}
        manifest = json.loads((ROOT / "public/assets/clubs/attribution.json").read_text(encoding="utf-8"))
        compatibility = {row["crest_url"]: row for row in manifest["compatibility_assets"]}
        current = {row["id"]: row for row in CLUBS}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-assets.sqlite3"
            store = Store(path)
            for team_id, crest_url in legacy_paths.items():
                store.put("teams", {"id": team_id, "name": "已保存的球队名称", "crest_url": crest_url,
                                    "profile_origin": "user", "profile_updated_at": "2026-09-12T03:00:00Z"})
            store.close()
            reopened = Store(path)
            seed(reopened)
            for team_id, crest_url in legacy_paths.items():
                with self.subTest(team=team_id):
                    self.assertEqual(reopened.get("teams", team_id)["crest_url"], crest_url)
                    self.assertEqual(reopened.get("teams", team_id)["profile_origin"], "user")
                    self.assertNotEqual(current[team_id]["crest_url"], crest_url)
                    body = (ROOT / "public" / crest_url.lstrip("/")).read_bytes()
                    self.assertEqual(hashlib.sha256(body).hexdigest(), compatibility[crest_url]["sha256"])
                    self.assertEqual(len(body), compatibility[crest_url]["bytes"])
            reopened.close()


if __name__ == "__main__":
    unittest.main()
