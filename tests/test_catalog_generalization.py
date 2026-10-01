"""Whole-catalog contracts, with cold stores and source-controlled local data.

These are offline capability checks, not claims of live publisher availability.
"""
import copy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from server import research, team_center
from server.club_library import CLUBS
from server.seed import seed
from server.storage import Store
from tests.support import IsolatedAppCase


class CatalogCapabilitiesTests(IsolatedAppCase):
    def setUp(self):
        super().setUp()
        self.app.state.collection_scheduler.enabled = False

    def test_every_catalog_club_has_read_only_views_and_the_same_preparation_route(self):
        store = self.app.state.store
        self.assertGreaterEqual(len(CLUBS), 96)
        initial_following = store.settings()["followed_teams"]
        initial_reports = store.list("reports")
        initial_runs = store.list("runs")
        manager = self.app.state.preparation
        with patch.object(manager, "start", return_value={"started": True, "status": "running"}) as start:
            for index, club in enumerate(CLUBS):
                identity = club["id"]
                with self.subTest(team=identity):
                    center = self.client.get(f"/api/teams/{identity}/center")
                    feed = self.client.get(f"/api/feed?team_id={identity}")
                    state = self.client.get(f"/api/teams/{identity}/preparation?kind=news")
                    self.assertEqual((center.status_code, feed.status_code, state.status_code), (200, 200, 200))
                    self.assertEqual(center.json()["team"]["id"], identity)
                    self.assertEqual(state.json()["status"], "idle")
                    self.assertEqual(start.call_count, 2 * index)
                    for kind in ("team", "news"):
                        response = self.client.post(f"/api/teams/{identity}/prepare", json={"kind": kind})
                        self.assertEqual(response.status_code, 202)
                        self.assertEqual(start.call_args.args, (kind, identity))
        self.assertEqual(store.settings()["followed_teams"], initial_following)
        self.assertEqual(store.list("reports"), initial_reports)
        self.assertEqual(store.list("runs"), initial_runs)


class CatalogColdDataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def cold_store(self, name, followed):
        store = Store(Path(self.directory.name) / (name + ".sqlite3"))
        self.addCleanup(store.close)
        seed(store)
        store.save_settings({"followed_teams": followed, "spoiler_free": False})
        return store

    async def test_all_catalog_rosters_and_source_id_photos_use_the_same_cold_path(self):
        store = self.cold_store("all-clubs", [])
        source_payloads = {}
        expected = {}
        for index, club in enumerate(CLUBS):
            team, league, season, provider_id = team_center.team_season_context(store, club["id"])
            player_id = str(700001 + index)
            image = f"https://a.espncdn.com/i/headshots/soccer/players/full/{player_id}.png"
            url = (f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league['provider_code']}"
                   f"/teams/{provider_id}/roster?season={season['season_start_year']}")
            source_payloads[url] = {"season": {"year": season["season_start_year"]},
                "team": {"id": provider_id}, "league": {"slug": league["provider_code"]},
                "athletes": [{"id": player_id, "displayName": f"Synthetic Catalog Player {index}",
                              "position": {"abbreviation": "M"}, "headshot": {"href": image}}]}
            expected[team["id"]] = (player_id, image, season["id"])

        async def fetch(url):
            self.assertIn(url, source_payloads)
            return copy.deepcopy(source_payloads[url])

        with patch("server.league_data.fetch_json", new=AsyncMock(side_effect=fetch)) as data_source, \
             patch("server.provider.request_completion", new=AsyncMock()) as model:
            for club in CLUBS:
                identity = club["id"]
                with self.subTest(team=identity):
                    result = await team_center.sync_roster(store, identity)
                    self.assertFalse(result["cached"])
                    view = team_center.center_view(store, identity)
                    player_id, image, season_id = expected[identity]
                    self.assertEqual(len(view["roster"]), 1)
                    player = view["roster"][0]
                    self.assertEqual((player["team_id"], player["season_id"], player["provider_id"]),
                                     (identity, season_id, player_id))
                    self.assertEqual(player["photo_url"], image)
                    self.assertEqual(player["photo_source_url"], player["source_url"])
                    self.assertEqual(player["photo_match"], "espn_provider_id")
                    self.assertNotIn("name_zh", player)
            self.assertEqual(data_source.await_count, len(CLUBS))
            model.assert_not_awaited()
        self.assertEqual(store.settings()["followed_teams"], [])
        self.assertEqual(store.list("runs"), [])

    async def test_following_does_not_change_same_source_discovery_or_readable_material(self):
        identity = "getafe"
        article_url = "https://www.getafecf.com/noticias/controlled-first-team-update"
        published = datetime.now(timezone.utc).isoformat()
        text = ("Getafe first-team players returned to the training ground for a controlled session. "
                "The coaching staff confirmed the session took place and did not announce a match squad. ") * 5
        body = (f'<html><head><meta property="article:published_time" content="{published}"></head>'
                f'<body><article><h1>Getafe first team training update</h1><p>{text}</p></article></body></html>')

        async def index(store, team_id, source, **kwargs):
            entries = [{"url": article_url, "title": "Getafe first team training update",
                        "description": "Public first-team training report", "published_at": published}] if source["id"] == "official_getafe" else []
            return entries, [], {"state": "fresh", "checked_at": published}

        empty_web = ([], [], {"id": "bing_news", "name": "公开新闻搜索", "state": "fresh", "checked_at": published})
        empty_espn = ([], [], {"id": "espn_news", "name": "ESPN 新闻", "state": "fresh", "checked_at": published})
        outcomes = []
        with patch("server.research._source_index", new=AsyncMock(side_effect=index)), \
             patch("server.research._web_news_index", new=AsyncMock(return_value=empty_web)), \
             patch("server.research._espn_news_index", new=AsyncMock(return_value=empty_espn)), \
             patch("server.research._fetch_shared", new=AsyncMock(return_value=(body, article_url, {}))), \
             patch("server.provider.request_completion", new=AsyncMock()) as model, \
             patch("server.news_reports.enqueue_reports") as enqueue:
            for followed in ([], [identity]):
                store = self.cold_store("followed" if followed else "unfollowed", followed)
                before = [source["id"] for source in research.source_catalog(store, identity)]
                result = await research.collect_team(store, identity, limit=1, max_attempts=2, max_age_days=7)
                self.assertEqual(len(result["evidence_ids"]), 1)
                evidence = store.get("evidence", result["evidence_ids"][0])
                self.assertEqual(evidence["team_id"], identity)
                self.assertEqual(urlsplit(evidence["url"]).hostname, "www.getafecf.com")
                self.assertEqual(store.settings()["followed_teams"], followed)
                self.assertEqual(store.list("reports"), [])
                self.assertEqual(store.list("runs"), [])
                outcomes.append({"source_ids": before,
                    "evidence": {field: evidence.get(field) for field in ("title", "url", "body_hash", "source_id", "published_at")},
                    "counts": [{field: source.get(field) for field in ("id", "indexed_count", "relevant_count",
                                "dated_count", "window_count", "read_attempted", "readable_count")} for source in result["sources"]]})
            model.assert_not_awaited()
            enqueue.assert_not_called()
        self.assertEqual(outcomes[0], outcomes[1])
