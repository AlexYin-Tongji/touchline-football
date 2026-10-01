"""Public ESPN page fallback keeps team, season and displayed-number validation."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from server import espn_pages, league_data, team_center
from server.ingestion import IngestionError
from tests.test_team_schedule import LEAGUE, SEASON, MAPPING


API = "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/teams/83/"
TEAM = {"id": "barcelona", "name": "Barcelona"}


def roster():
    return {"metadata": {"year": 2026, "season": "2026-27", "league": "Spanish LALIGA"},
        "leaguesDropdown": [{"value": "ESP.1", "label": "Spanish LALIGA"}], "team": {"id": "83"}, "groups": [
        {"athletes": [{"uid": "s:600~a:123", "href": "https://www.espn.com/soccer/player/_/id/123/example-player",
                       "name": "Example Player", "jersey": "8", "position": "M", "age": "25", "ctz": "Spain",
                       "height": "6' 0\"", "weight": "160 lbs", "appearances": "7", "totalGoals": "--"}]}]}


def schedule(*, completed=True):
    return {"season": {"year": 2026}, "team": {"id": "83"}, "seasonTypeYear": "2026-27",
            "competitionName": "Spanish LALIGA", "dropdownLeagues": [{"value": "ESP.1", "title": "Spanish LALIGA"}],
            "events": [{"id": "456", "date": "2026-09-13T14:15Z", "tbd": False,
                        "completed": completed, "score": "2 - 4" if completed else "",
                        "status": {"state": "post" if completed else "pre", "detail": "FT" if completed else "TBD"},
                        "league": "Spanish LALIGA", "competitors": [
                            {"id": "1538", "displayName": "Levante", "isHome": True, "score": 0},
                            {"id": "83", "displayName": "Barcelona", "isHome": False, "score": 0, "winner": completed}]}]}


def standings():
    names = ["gamesplayed", "wins", "ties", "losses", "pointsfor", "pointsagainst", "pointdifferential", "points", "rank"]
    return {"leagueUid": "s:600~l:740", "requestedDates": {"season": 2026}, "currentSeason": {"year": 2026},
            "groups": {"headers": {name: {"t": name, "i": index} for index, name in enumerate(names)}, "groups": [
                {"standings": [
                    {"team": {"id": "83", "uid": "s:600~t:83"}, "stats": ["1", "1", "0", "0", "4", "2", "+2", "3", "1"]},
                    {"team": {"id": "1538", "uid": "s:600~t:1538"}, "stats": ["1", "0", "0", "1", "2", "4", "-2", "0", "2"]}]}]}}


def document(kind="squad", data=None):
    params = {"sport": "soccer", "league": "ESP.1", "season": "2026", "pageType": "standings" if kind == "standings" else "team",
              "subPageType": None if kind == "standings" else kind}
    if kind != "standings":
        params["id"] = "83"
    payload = {"routing": {"params": params}, "page": {"type": params["pageType"], "subType": params["subPageType"],
                "content": {kind: data if data is not None else roster()}}}
    return payload


def html(payload):
    return "<html><script>window['__espnfitt__']=" + json.dumps(payload) + ";</script></html>"


def portrait_document(identity="123"):
    row = {"id": identity, "uid": "s:600~a:" + identity, "name": "Example Player",
           "href": "/soccer/player/_/id/" + identity + "/example-player",
           "headshot": "https://a.espncdn.com/i/headshots/soccer/players/full/" + identity + ".png"}
    return {"routing": {"params": {"sport": "soccer", "pageType": "player", "id": identity}},
            "page": {"type": "player", "content": {"player": {"playerId": identity, "sport": "soccer",
                "plyrSwtch": {"cur": row, "lst": [row]},
                "nws": {"items": [{"image": {"url": "https://a.espncdn.com/photo/wrong-news-image.jpg"}}]}}}}}


class PublicPageParserTests(unittest.TestCase):
    def test_player_photos_only_use_identity_bound_selector_records(self):
        records = espn_pages.parse_player_portrait_records(html(portrait_document()), "123")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], "123")
        self.assertIn("/players/full/123.png", records[0]["headshot"]["href"])
        for field, wrong in (("uid", "s:600~a:456"), ("href", "/soccer/player/_/id/456/other"),
                             ("href", "https://evil.test/soccer/player/_/id/123/example")):
            payload = portrait_document()
            payload["page"]["content"]["player"]["plyrSwtch"]["cur"][field] = wrong
            self.assertEqual(espn_pages.parse_player_portrait_records(html(payload), "123"), [])
        payload = portrait_document()
        payload["routing"]["params"]["id"] = "456"
        with self.assertRaises(IngestionError):
            espn_pages.parse_player_portrait_records(html(payload), "123")

    def parse(self, payload, endpoint="roster?season=2026"):
        return espn_pages.parse_page(html(payload), espn_pages.fallback_spec(API + endpoint))

    def test_all_competitions_public_page_keeps_explicit_european_events(self):
        data = schedule()
        data["competitionName"] = "All Competitions"
        data["dropdownLeagues"] += [{"value": "all", "title": "All Competitions"},
                                   {"value": "UEFA.CHAMPIONS", "title": "UEFA Champions League"}]
        cup = copy.deepcopy(data["events"][0])
        cup.update(id="789", league="UEFA Champions League")
        data["events"].append(cup)
        page = document("results", data)
        page["routing"]["params"]["league"] = "all"
        spec = espn_pages.fallback_spec(API.replace("esp.1", "all") + "schedule?season=2026")
        normalized = espn_pages.parse_page(html(page), spec)
        self.assertEqual([r["league"]["slug"] for r in normalized["events"]], ["esp.1", "uefa.champions"])
        page["page"]["content"]["results"]["events"][1]["league"] = "Unidentified Cup"
        with self.assertRaises(IngestionError):
            espn_pages.parse_page(html(page), spec)

    def test_explicit_roster_identity_and_missing_values_survive_existing_parser(self):
        normalized = self.parse(document())
        snapshot = team_center.parse_roster(normalized, TEAM, LEAGUE, SEASON, "83")
        player = snapshot["roster"][0]
        self.assertEqual(player["provider_id"], "123")
        self.assertEqual(player["height_cm"], 183)
        self.assertEqual(player["stats"]["appearances"], 7)
        self.assertIsNone(player["stats"]["goals"])
        self.assertEqual(player["photo_url"], "")

    def test_wrong_route_team_content_team_season_and_player_identity_are_rejected(self):
        mutations = [
            lambda p: p["routing"]["params"].update(id="359"),
            lambda p: p["routing"]["params"].update(season="2025"),
            lambda p: p["page"]["content"]["squad"]["team"].update(id="359"),
            lambda p: p["page"]["content"]["squad"]["metadata"].update(year=2025),
            lambda p: p["page"]["content"]["squad"]["metadata"].update(league="Another competition"),
            lambda p: p["page"]["content"]["squad"]["groups"][0]["athletes"][0].update(uid="s:600~a:999"),
        ]
        for mutate in mutations:
            payload = document()
            mutate(payload)
            with self.subTest(mutation=mutate), self.assertRaises(IngestionError):
                self.parse(payload)

    def test_schedule_uses_explicit_display_score_instead_of_zero_placeholders(self):
        normalized = self.parse(document("results", schedule()), "schedule?season=2026")
        row, = league_data.parse_team_schedule(normalized, LEAGUE, SEASON, MAPPING, "83")
        self.assertEqual((row["home_score"], row["away_score"]), (2, 4))
        damaged = schedule()
        damaged["events"][0]["score"] = ""
        with self.assertRaises(IngestionError):
            self.parse(document("results", damaged), "schedule?season=2026")
        reversed_home_away = schedule()
        reversed_home_away["events"][0]["competitors"].reverse()
        with self.assertRaises(IngestionError):
            self.parse(document("results", reversed_home_away), "schedule?season=2026")
        future = self.parse(document("fixtures", schedule(completed=False)), "schedule?season=2026&fixture=true")
        row, = league_data.parse_team_schedule(future, LEAGUE, SEASON, MAPPING, "83")
        self.assertIsNone(row["home_score"])
        self.assertIsNone(row["away_score"])

    def test_standings_read_named_column_indices_and_keep_missing_distinct_from_zero(self):
        spec = espn_pages.fallback_spec("https://site.api.espn.com/apis/v2/sports/soccer/esp.1/standings?season=2026")
        parsed = espn_pages.parse_page(html(document("standings", standings())), spec)
        rows = league_data.parse_standings(parsed, LEAGUE, SEASON, MAPPING)
        self.assertEqual(rows[0]["goal_difference"], 2)
        self.assertEqual(rows[1]["points"], 0)
        damaged = standings()
        damaged["groups"]["groups"][0]["standings"][0]["stats"][0] = "--"
        with self.assertRaises(IngestionError):
            espn_pages.parse_page(html(document("standings", damaged)), spec)

    def test_unknown_endpoints_untrusted_hosts_and_executable_payloads_are_rejected(self):
        for url in (API + "roster?season=2026&redirect=https://evil.example", API + "roster?season=2026&season=2025",
                    "https://evil.example/apis/site/v2/sports/soccer/esp.1/teams/83/roster?season=2026",
                    "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard?dates=2026"):
            self.assertIsNone(espn_pages.fallback_spec(url))
        with self.assertRaises(IngestionError):
            espn_pages.parse_page("<script>window['__espnfitt__']=(function(){throw 1})()</script>", espn_pages.fallback_spec(API + "roster?season=2026"))


class PublicPageFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for module in (league_data, espn_pages):
            offline = patch.object(module, "getproxies", return_value={})
            offline.start()
            self.addCleanup(offline.stop)

    async def test_known_api_transport_failure_reaches_independent_public_page(self):
        for failure in (httpx.ConnectTimeout, httpx.ConnectError):
            calls = []
            def respond(request):
                calls.append(str(request.url))
                if request.url.host == "site.api.espn.com":
                    raise failure("safe mock transport failure", request=request)
                return httpx.Response(200, text=html(document()), headers={"content-type": "text/html"})
            real_client = httpx.AsyncClient
            with self.subTest(failure=failure), patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs:
                    real_client(transport=httpx.MockTransport(respond), **kwargs)):
                result = await league_data.fetch_json(API + "roster?season=2026")
            self.assertEqual(result["team"]["id"], "83")
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[-1], espn_pages.fallback_spec(API + "roster?season=2026")["url"])

    async def test_platform_proxy_is_applied_without_environment_bypass(self):
        real_client = httpx.AsyncClient
        def client(**kwargs):
            self.assertEqual(kwargs.pop("proxy"), "http://127.0.0.1:7897")
            self.assertFalse(kwargs["trust_env"])
            return real_client(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, json={"athletes": []}) if request.url.host == "site.api.espn.com" else
                httpx.Response(200, text=html(document()), headers={"content-type": "text/html"})), **kwargs)
        with patch.object(league_data, "getproxies", return_value={"https": "http://127.0.0.1:7897"}), \
             patch.object(espn_pages, "getproxies", return_value={"https": "http://127.0.0.1:7897"}), \
             patch.object(httpx, "AsyncClient", side_effect=client) as constructed:
            await league_data.fetch_json(API + "roster?season=2026")
            await espn_pages.fetch_public_payload(espn_pages.fallback_spec(API + "roster?season=2026"))
        self.assertEqual(constructed.call_count, 2)

    async def test_cancelled_api_does_not_start_a_public_page(self):
        def respond(request):
            raise asyncio.CancelledError()
        real_client = httpx.AsyncClient
        with patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs:
                real_client(transport=httpx.MockTransport(respond), **kwargs)), \
             patch.object(espn_pages, "fetch_public_payload", new=AsyncMock()) as fallback:
            with self.assertRaises(asyncio.CancelledError):
                await league_data.fetch_json(API + "roster?season=2026")
        fallback.assert_not_called()

    async def test_known_api_403_uses_one_fixed_public_page_and_validates_it(self):
        calls = []
        def respond(request):
            calls.append(str(request.url))
            if request.url.host == "site.api.espn.com":
                return httpx.Response(403, text="Access Denied")
            return httpx.Response(200, text=html(document()), headers={"content-type": "text/html"})
        real_client = httpx.AsyncClient
        with patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)):
            result = await league_data.fetch_json(API + "roster?season=2026")
        self.assertEqual(result["team"]["id"], "83")
        self.assertEqual(calls, [API + "roster?season=2026", "https://www.espn.com/soccer/team/squad/_/id/83/league/esp.1/season/2026"])

    async def test_unknown_api_403_does_not_follow_a_public_page(self):
        calls = []
        def respond(request):
            calls.append(str(request.url))
            return httpx.Response(403)
        real_client = httpx.AsyncClient
        with patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)):
            with self.assertRaises(IngestionError):
                await league_data.fetch_json("https://site.api.espn.com/apis/test")
        self.assertEqual(len(calls), 1)

    async def test_redirected_public_page_is_not_followed_to_another_source(self):
        calls = []
        def respond(request):
            calls.append(str(request.url))
            return httpx.Response(403) if request.url.host == "site.api.espn.com" else httpx.Response(302, headers={"location": "https://elsewhere.example/login"})
        real_client = httpx.AsyncClient
        with patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)):
            with self.assertRaises(IngestionError):
                await league_data.fetch_json(API + "roster?season=2026")
        self.assertEqual(len(calls), 2)

    async def test_oversize_public_page_is_rejected_without_parsing_or_retries(self):
        def respond(request):
            return httpx.Response(200, content=b"x" * (espn_pages.MAX_BODY + 1), headers={"content-type": "text/html"})
        real_client = httpx.AsyncClient
        with patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)):
            with self.assertRaises(IngestionError):
                await espn_pages.fetch_public_payload(espn_pages.fallback_spec(API + "roster?season=2026"))
