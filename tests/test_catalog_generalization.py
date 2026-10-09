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


def baseline_row(club, profile, view):
    """One club's baseline as the review sheet reports it.

    Every field is either a value or an explicit reason it is absent. There is
    no default that reads as success: a club whose squad was never fetched and a
    club whose squad is genuinely short both have to be distinguishable, because
    "we did not look" and "there is nothing there" are different findings.
    """
    import hashlib
    from server.club_library import CLUBS as _all

    root = Path(__file__).resolve().parents[1] / "public"
    crest = root / str(profile.get("crest_url") or "").lstrip("/")
    roster = view.get("roster") or []
    standings = view.get("standings") or []
    matches = view.get("matches") or []
    photos = [player for player in roster if player.get("photo_url")]
    photos_attributed = [player for player in photos
                         if player.get("photo_credit") and player.get("photo_source_url")]
    return {
        "club": club["id"],
        "crest_path": profile.get("crest_url"),
        "crest_ok": crest.is_file(),
        "crest_bytes": crest.stat().st_size if crest.is_file() else 0,
        "crest_sha256": hashlib.sha256(crest.read_bytes()).hexdigest() if crest.is_file() else "",
        "crest_format": crest.suffix.lower(),
        "founded": profile.get("founded"),
        "stadium": profile.get("stadium"),
        "nickname": profile.get("nickname"),
        "official_url": profile.get("official_url"),
        "roster": len(roster),
        "roster_ready": bool(roster),
        "photos": len(photos),
        "photos_attributed": len(photos_attributed),
        "standings_row": next((row for row in standings if row.get("is_current")), None) is not None,
        "matches": len(matches),
        "season": (view.get("season") or {}).get("id"),
    }


class CatalogBaselineTests(unittest.IsolatedAsyncioTestCase):
    """Every club's data path, asserted one club at a time.

    The catalogue holds 96 clubs and the application is expected to serve all of
    them, so a check that passes for six popular clubs says nothing about the
    other ninety. Each club gets its own assertions, its own synthetic squad and
    its own entry in a machine-readable sheet, and a club that cannot be read is
    recorded as a failure of that club rather than absorbed into a total.

    The sources are faked — this is an offline contract, not a claim that any
    publisher is reachable today. What it does prove is that the *shape* of the
    result is the same for Saint-Étienne as for Real Madrid: same fields, same
    attribution rules, same explicit gap when a squad is missing.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def cold_store(self, name):
        store = Store(Path(self.directory.name) / (name + ".sqlite3"))
        self.addCleanup(store.close)
        seed(store)
        store.save_settings({"followed_teams": [], "spoiler_free": False})
        return store

    async def test_every_club_has_a_crest_squad_and_explicit_gaps(self):
        store = self.cold_store("baseline")
        self.assertGreaterEqual(len(CLUBS), 96)
        payloads, expected = {}, {}
        for index, club in enumerate(CLUBS):
            team, league, season, provider_id = team_center.team_season_context(store, club["id"])
            player_id = str(800001 + index)
            url = (f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league['provider_code']}"
                   f"/teams/{provider_id}/roster?season={season['season_start_year']}")
            payloads[url] = {"season": {"year": season["season_start_year"]},
                "team": {"id": provider_id}, "league": {"slug": league["provider_code"]},
                "athletes": [{"id": player_id, "displayName": f"Baseline Player {index}",
                              "jersey": "9", "position": {"abbreviation": "M"}}]}
            expected[team["id"]] = player_id

        async def fetch(url):
            self.assertIn(url, payloads)
            return copy.deepcopy(payloads[url])

        rows = []
        with patch("server.league_data.fetch_json", new=AsyncMock(side_effect=fetch)), \
             patch("server.provider.request_completion", new=AsyncMock()) as model:
            for club in CLUBS:
                identity = club["id"]
                with self.subTest(club=identity):
                    stored = store.get("teams", identity) or {}
                    await team_center.sync_roster(store, identity)
                    view = team_center.center_view(store, identity)
                    row = baseline_row(club, stored, view)
                    rows.append(row)

                    self.assertTrue(row["crest_ok"], f"{identity} 的队徽文件不存在：{row['crest_path']}")
                    self.assertTrue(row["crest_path"].startswith("/assets/clubs/"))
                    self.assertGreater(row["crest_bytes"], 1000)
                    # A squad read must produce that club's own player, with the
                    # provider identity the catalogue claims, or say it has none.
                    self.assertEqual(row["season"], view["season"]["id"])
                    if row["roster"]:
                        self.assertEqual(view["roster"][0]["provider_id"], expected[identity])
                        self.assertEqual(view["roster"][0]["team_id"], identity)
                    else:
                        self.assertTrue(any("阵容" in error or "roster" in error.lower()
                                            for error in view.get("errors") or []),
                                        f"{identity} 没有阵容，也没有说明原因")
                    # No photo is not a failure, but a photo without an
                    # attribution is: it cannot be shown to a reader.
                    self.assertEqual(row["photos"], row["photos_attributed"])
                    for field in ("official_url", "crest_path"):
                        self.assertTrue(row[field], f"{identity} 缺少 {field}")
            model.assert_not_awaited()

        # The sheet is the deliverable: one line per club, and every club is in
        # it. A run that quietly covered 90 clubs is indistinguishable from a
        # complete one unless the count is asserted.
        self.assertEqual(len(rows), len(CLUBS))
        self.assertEqual({row["club"] for row in rows}, {club["id"] for club in CLUBS})
        self.assertEqual(store.settings()["followed_teams"], [])
        self.assertEqual(store.list("runs"), [])

    async def test_background_fields_are_either_stated_or_absent_never_zero(self):
        """A missing founding year is missing, not 0 and not an empty string.

        The catalogue is filled from a reviewed offline pass, so a club can
        legitimately have no founding year. What must never happen is a
        placeholder: `founded: 0` renders as "创立于 0 年", and `stadium: ""`
        renders as an empty fact row.
        """
        for club in CLUBS:
            with self.subTest(club=club["id"]):
                if "founded" in club:
                    self.assertIsInstance(club["founded"], int)
                    self.assertGreater(club["founded"], 1800)
                    self.assertLess(club["founded"], 2100)
                for field in ("stadium", "nickname"):
                    if field in club:
                        self.assertIsInstance(club[field], str)
                        self.assertTrue(club[field].strip(), f"{club['id']} 的 {field} 是空串")
                self.assertNotIn("founded", {k: v for k, v in club.items() if v == 0})
