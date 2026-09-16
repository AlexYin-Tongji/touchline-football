"""Season, data-loss, privacy, and per-team regressions for the Big Five feed."""
import asyncio
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from server import fixtures, ingestion, league_data
from server.league_catalog import LEAGUES, MEMBERSHIPS, SEASONS
from server.storage import Store


LEAGUE = {"id": "PL", "name": "英超", "provider_code": "eng.1",
          "provider_league_uid": "s:600~l:700", "expected_team_count": 2}
SEASON = {"id": "PL-2026", "league_id": "PL", "season_start_year": 2026, "label": "2026/27"}
MAPPING = {"359": "arsenal", "363": "chelsea"}


def scoreboard(*, state="finished", scores=(2, 1), year=2026, match_id="820001", time_valid=True,
               home_id="359", away_id="363", date="2026-09-11T19:00:00Z"):
    states = {"finished": {"name": "STATUS_FULL_TIME", "state": "post", "completed": True},
              "scheduled": {"name": "STATUS_SCHEDULED", "state": "pre", "completed": False},
              "live": {"name": "STATUS_IN_PROGRESS", "state": "in", "completed": False},
              "postponed": {"name": "STATUS_POSTPONED", "state": "pre", "completed": False},
              "cancelled": {"name": "STATUS_CANCELED", "state": "post", "completed": False}}
    status = {"type": states[state]}
    game = {"id": match_id, "date": date, "timeValid": time_valid, "status": status,
            "venue": {"fullName": "Example Stadium"}, "competitors": [
                {"id": home_id, "type": "team", "homeAway": "home", "team": {"displayName": "Arsenal"}, "score": scores[0]},
                {"id": away_id, "type": "team", "homeAway": "away", "team": {"displayName": "Chelsea"}, "score": scores[1]}]}
    return {"leagues": [{"slug": "eng.1", "uid": LEAGUE["provider_league_uid"], "season": {"year": year}}],
            "events": [{"id": match_id, "date": date, "season": {"year": year}, "status": status, "competitions": [game]}]}


def standings(*, year=2026, mapping=None, uid=None):
    mapping = mapping or MAPPING
    entries = []
    for rank, provider_id in enumerate(mapping, 1):
        stats = {"rank": rank, "gamesPlayed": 3, "wins": 1, "ties": 1, "losses": 1,
                 "pointsFor": 4, "pointsAgainst": 4, "pointDifferential": 0, "points": 4}
        entries.append({"team": {"id": provider_id}, "stats": [{"name": key, "value": value} for key, value in stats.items()]})
    return {"season": {"year": year}, "uid": uid or LEAGUE["provider_league_uid"],
            "children": [{"standings": {"entries": entries}}]}


class LeagueParserTests(unittest.TestCase):
    def test_finished_result_and_utc_are_parsed(self):
        row, = league_data.parse_scoreboard(scoreboard(), LEAGUE, SEASON, MAPPING)
        self.assertEqual((row["home_team_id"], row["away_team_id"]), ("arsenal", "chelsea"))
        self.assertEqual((row["home_score"], row["away_score"]), (2, 1))
        self.assertEqual(row["kickoff"], "2026-09-11T19:00:00+00:00")
        self.assertTrue(row["kickoff_confirmed"])
        self.assertEqual(row["season_id"], "PL-2026")

    def test_unfinished_zero_placeholders_and_live_scores_are_withheld(self):
        for state in ("scheduled", "live", "postponed", "cancelled"):
            with self.subTest(state=state):
                row, = league_data.parse_scoreboard(scoreboard(state=state, scores=("0", "0")), LEAGUE, SEASON, MAPPING)
                self.assertEqual(row["status"], state)
                self.assertIsNone(row["home_score"])
                self.assertIsNone(row["away_score"])
                if state in {"postponed", "cancelled"}:
                    self.assertFalse(row["kickoff_confirmed"])
        row, = league_data.parse_scoreboard(scoreboard(state="live", scores=(5, 1)), LEAGUE, SEASON, MAPPING)
        self.assertIsNone(row["home_score"])

    def test_unconfirmed_time_keeps_date_without_claiming_confirmation(self):
        row, = league_data.parse_scoreboard(scoreboard(time_valid=False), LEAGUE, SEASON, MAPPING)
        self.assertFalse(row["kickoff_confirmed"])
        self.assertEqual(row["kickoff_date"], "2026-09-11")

    def test_fixture_league_and_season_identity_are_required(self):
        wrong_league = scoreboard()
        wrong_league["leagues"][0]["slug"] = "esp.1"
        mixed = scoreboard()
        mixed["events"][0]["season"]["year"] = 2025
        for payload in (wrong_league, scoreboard(year=2025), mixed):
            with self.subTest(payload=payload):
                with self.assertRaises(ingestion.IngestionError):
                    league_data.parse_scoreboard(payload, LEAGUE, SEASON, MAPPING)

    def test_unknown_duplicate_or_non_team_participants_are_rejected(self):
        invalid_type = scoreboard()
        invalid_type["events"][0]["competitions"][0]["competitors"][0]["type"] = "athlete"
        for payload in (scoreboard(away_id="9999"), scoreboard(away_id="359"), invalid_type):
            with self.assertRaises(ingestion.IngestionError):
                league_data.parse_scoreboard(payload, LEAGUE, SEASON, MAPPING)

    def test_duplicate_identical_match_collapses_but_conflicts_fail(self):
        payload = scoreboard()
        payload["events"].append(copy.deepcopy(payload["events"][0]))
        self.assertEqual(len(league_data.parse_scoreboard(payload, LEAGUE, SEASON, MAPPING)), 1)
        payload["events"][1]["competitions"][0]["competitors"][0]["score"] = 3
        with self.assertRaises(ingestion.IngestionError):
            league_data.parse_scoreboard(payload, LEAGUE, SEASON, MAPPING)

    def test_empty_fixture_response_cannot_replace_saved_games(self):
        payload = scoreboard()
        payload["events"] = []
        with self.assertRaises(ingestion.IngestionError):
            league_data.parse_scoreboard(payload, LEAGUE, SEASON, MAPPING)

    def test_finished_scores_must_be_complete_nonnegative_integers(self):
        for scores in ((None, 1), ("bad", 1), (True, 0), (1.5, 0), (-1, 0)):
            with self.subTest(scores=scores), self.assertRaises(ingestion.IngestionError):
                league_data.parse_scoreboard(scoreboard(scores=scores), LEAGUE, SEASON, MAPPING)

    def test_standings_validate_full_membership_and_arithmetic(self):
        rows = league_data.parse_standings(standings(), LEAGUE, SEASON, MAPPING)
        self.assertEqual([row["rank"] for row in rows], [1, 2])
        partial = standings()
        partial["children"][0]["standings"]["entries"].pop()
        duplicate = standings()
        duplicate["children"][0]["standings"]["entries"][1]["team"]["id"] = "359"
        arithmetic = standings()
        arithmetic["children"][0]["standings"]["entries"][0]["stats"][1]["value"] = 12
        for payload in (partial, duplicate, arithmetic, standings(year=2025), standings(uid="s:600~l:740")):
            with self.assertRaises(ingestion.IngestionError):
                league_data.parse_standings(payload, LEAGUE, SEASON, MAPPING)

    def test_negative_points_are_allowed_for_disciplinary_deductions(self):
        payload = standings()
        points = next(s for s in payload["children"][0]["standings"]["entries"][0]["stats"] if s["name"] == "points")
        points["value"] = -2
        self.assertEqual(league_data.parse_standings(payload, LEAGUE, SEASON, MAPPING)[0]["points"], -2)

    def test_every_real_league_identity_accepts_its_full_roster(self):
        for league in LEAGUES:
            season = next(s for s in SEASONS if s["league_id"] == league["id"])
            mapping = {m["provider_team_id"]: m["team_id"] for m in MEMBERSHIPS if m["season_id"] == season["id"]}
            payload = standings(mapping=mapping, uid=league["provider_league_uid"])
            with self.subTest(league=league["id"]):
                rows = league_data.parse_standings(payload, league, season, mapping)
                self.assertEqual(len(rows), league["expected_team_count"])
                self.assertEqual({r["team_id"] for r in rows}, set(mapping.values()))


class LeagueStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "leagues.sqlite3")
        self.store.put("leagues", LEAGUE)
        self.store.put("seasons", SEASON)
        for provider_id, team_id in MAPPING.items():
            self.store.put("teams", {"id": team_id, "name": "阿森纳" if team_id == "arsenal" else "切尔西",
                                     "english_name": "Arsenal" if team_id == "arsenal" else "Chelsea"})
            self.store.put("league_memberships", {"id": "PL-2026:" + team_id, "league_id": "PL", "season_id": "PL-2026",
                                                 "season_start_year": 2026, "provider_team_id": provider_id, "team_id": team_id})
        self.store.save_settings({"followed_teams": [], "spoiler_free": True})

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def sync(self, first=None, second=None):
        with patch("server.league_data.fetch_json", new=AsyncMock(side_effect=[first or scoreboard(), second or standings()])):
            return await league_data.sync_league(self.store, "PL", 2026, force=True)

    async def test_complete_sync_and_failure_preserve_last_successful_snapshot(self):
        initial = await self.sync()
        self.assertEqual(initial["sync_status"], "completed")
        before = {name: self.store.list(name) for name in ("league_matches", "standings")}
        failure = await self.sync(ingestion.IngestionError("fixture outage"), ingestion.IngestionError("table outage"))
        self.assertEqual(failure["sync_status"], "failed")
        self.assertEqual(failure["last_sync"], initial["last_sync"])
        self.assertEqual(failure["fixtures_at"], initial["fixtures_at"])
        self.assertEqual(len(failure["errors"]), 2)
        self.assertEqual(before, {name: self.store.list(name) for name in before})

    async def test_partial_success_updates_only_the_available_source(self):
        initial = await self.sync()
        old_table = self.store.get("standings", "PL-2026")
        result = await self.sync(scoreboard(scores=(3, 2)), ingestion.IngestionError("unavailable table"))
        self.assertEqual(result["sync_status"], "partial")
        self.assertEqual(self.store.get("standings", "PL-2026"), old_table)
        self.assertEqual(result["standings_at"], initial["standings_at"])
        self.assertEqual(self.store.list("league_matches")[0]["home_score"], 3)
        self.assertEqual(len(result["errors"]), 1)

    async def test_stale_scheduled_or_damaged_result_cannot_erase_final_score(self):
        await self.sync()
        before = self.store.list("league_matches")
        for payload in (scoreboard(state="scheduled", scores=(0, 0)), scoreboard(scores=(None, 1))):
            result = await self.sync(payload)
            self.assertEqual(result["sync_status"], "partial")
            self.assertEqual(self.store.list("league_matches"), before)

    async def test_recent_sync_uses_cache_without_another_network_request(self):
        initial = await self.sync()
        with patch("server.league_data.fetch_json", new=AsyncMock()) as network:
            result = await league_data.sync_league(self.store, "PL", 2026)
            network.assert_not_awaited()
        self.assertTrue(result["cached"])
        self.assertEqual(result["last_sync"], initial["last_sync"])

    async def test_materialized_home_away_views_have_own_evidence_scope(self):
        await self.sync()
        self.assertEqual(league_data.materialize_team(self.store, "arsenal", "PL-2026"), 1)
        self.assertEqual(league_data.materialize_team(self.store, "chelsea", "PL-2026"), 1)
        home, = self.store.list("matches", "live", "arsenal")
        away, = self.store.list("matches", "live", "chelsea")
        self.assertEqual((home["score"], away["score"]), ("2–1", "1–2"))
        self.assertEqual((home["result"], away["result"]), ("W", "L"))
        self.assertEqual((home["opponent"], away["opponent"]), ("切尔西", "阿森纳"))
        self.assertNotEqual(home["evidence_ids"], away["evidence_ids"])
        for match in (home, away):
            evidence = self.store.get("evidence", match["evidence_ids"][0])
            self.assertEqual(evidence["team_id"], match["team_id"])
            self.assertEqual(evidence["match_id"], match["id"])
            self.assertTrue(evidence["fixture_record"] and evidence["spoiler"])
            self.assertIsNone(ingestion.safe_evidence_view(evidence))
        count = len(self.store.list("evidence"))
        league_data.materialize_team(self.store, "arsenal", "PL-2026")
        self.assertEqual(len(self.store.list("evidence")), count)

    async def test_scheduled_evidence_contains_no_numeric_score_placeholders(self):
        await self.sync(scoreboard(state="scheduled", scores=(0, 0)))
        league_data.materialize_team(self.store, "arsenal", "PL-2026")
        match, = self.store.list("matches", "live", "arsenal")
        evidence = self.store.get("evidence", match["evidence_ids"][0])
        self.assertIsNone(match["score"])
        self.assertFalse(evidence["spoiler"])
        raw = json.loads(evidence["text"].split("\n", 1)[1])
        self.assertIsNone(raw["home_score"])
        self.assertIsNone(raw["away_score"])
        self.assertIsNotNone(ingestion.safe_evidence_view(evidence))

    async def test_personal_match_fields_and_confirmed_lineups_survive_rematerialization(self):
        await self.sync()
        league_data.materialize_team(self.store, "arsenal", "PL-2026")
        match, = self.store.list("matches", "live", "arsenal")
        preserved = {"note": "我记录的观赛笔记", "availability": [{"player": "球员 A", "status": "待定"}],
                     "lineups": {"status": "confirmed", "home": [{"name": "球员 A"}]}, "personal_tag": "再看一次"}
        self.store.put("matches", {**match, **preserved})
        league_data.materialize_team(self.store, "arsenal", "PL-2026")
        updated = self.store.get("matches", match["id"])
        for key, value in preserved.items():
            self.assertEqual(updated.get(key), value, key)

    async def test_matching_direct_official_fixture_is_not_duplicated(self):
        await self.sync()
        original = {"id": "official_arsenal_chelsea", "mode": "live", "team_id": "arsenal", "opponent": "Chelsea",
                    "kickoff": "2026-09-11T19:00:00+00:00", "note": "官方资料", "evidence_ids": []}
        self.store.put("matches", original)
        self.assertEqual(league_data.materialize_team(self.store, "arsenal", "PL-2026"), 0)
        self.assertEqual(self.store.list("matches", "live", "arsenal"), [original])

    async def test_later_official_sync_adopts_existing_league_game_and_preserves_user_notes(self):
        await self.sync()
        league_data.materialize_team(self.store, "arsenal", "PL-2026")
        generated, = self.store.list("matches", "live", "arsenal")
        generated.update(note="先记录的观赛笔记", personal_tag="稍后重看")
        generated["evidence_ids"].append("manual_match_note")
        self.store.put("matches", generated)
        official = fixtures._base("arsenal", "official-arsenal-chelsea", "Arsenal", "Chelsea",
                                  "2026-09-11T19:00:00Z", "finished", home_score=3, away_score=1,
                                  competition="Premier League", source_url="https://www.arsenal.com/fixture/test")
        fixtures.save_matches(self.store, "arsenal", [official], "2026-09-12T06:00:00+00:00")
        league_data.materialize_team(self.store, "arsenal", "PL-2026")
        matches = self.store.list("matches", "live", "arsenal")
        self.assertEqual(len(matches), 1)
        match, = matches
        self.assertEqual(match["id"], generated["id"])
        self.assertEqual(match["score"], "3–1")
        self.assertEqual(match["note"], "先记录的观赛笔记")
        self.assertEqual(match["personal_tag"], "稍后重看")
        self.assertIn("manual_match_note", match["evidence_ids"])
        self.assertNotEqual(match.get("provider"), "ESPN")

    async def test_protected_league_hides_scores_and_standings_without_mutating_storage(self):
        await self.sync()
        before = self.store.list("league_matches")
        protected = league_data.league_view(self.store, "PL", 2026)
        self.assertTrue(protected["standings_hidden"])
        self.assertEqual(protected["standings"], [])
        self.assertIsNone(protected["fixtures"][0]["home_score"])
        self.assertTrue(protected["fixtures"][0]["spoiler_hidden"])
        self.assertEqual(self.store.list("league_matches"), before)
        self.store.save_settings({"spoiler_free": False})
        public = league_data.league_view(self.store, "PL", 2026)
        self.assertEqual(public["fixtures"][0]["home_score"], 2)
        self.assertEqual(len(public["standings"]), 2)

    async def test_latest_snapshot_coverage_does_not_count_older_leftover_games(self):
        payload = scoreboard()
        other = scoreboard(match_id="820002", date="2026-12-11T19:00:00Z", home_id="363", away_id="359")
        payload["events"] += other["events"]
        await self.sync(payload)
        self.assertTrue(league_data.league_view(self.store, "PL")["coverage"]["has_full_schedule"])
        await self.sync()
        view = league_data.league_view(self.store, "PL")
        self.assertEqual(view["coverage"]["matches"], 1)
        self.assertFalse(view["coverage"]["has_full_schedule"])

    async def test_team_sync_falls_back_to_league_for_other_supported_clubs(self):
        self.store.put("teams", {"id": "chelsea", "name": "切尔西"})
        expected = {"matches_updated": 4, "provider": "ESPN", "errors": []}
        with patch("server.league_data.sync_team", new=AsyncMock(return_value=expected)) as fallback:
            result = await fixtures.sync_team(self.store, "chelsea", force=True)
        fallback.assert_awaited_once_with(self.store, "chelsea", force=True)
        self.assertEqual(result, expected)


class LeagueAPITests(unittest.TestCase):
    def setUp(self):
        from server.main import create_app
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.directory.name) / "api-leagues.sqlite3")
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.client.put("/api/settings", json={"spoiler_free": True})
        self.store = self.app.state.store
        self.store.save_settings({"mode": "live", "reports": {"auto_generate": False}, "followed_teams": []})

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def populate(self):
        league, season, mapping = league_data.season_context(self.store, "PL", 2026)
        records = league_data.parse_scoreboard(scoreboard(), league, season, mapping)
        table = league_data.parse_standings(standings(mapping=mapping), league, season, mapping)
        self.store.put_many("league_matches", records)
        self.store.put("standings", {"id": "PL-2026", "league_id": "PL", "season_id": "PL-2026", "rows": table,
                                     "fetched_at": "2026-09-12T04:00:00+00:00"})

    def test_league_endpoint_exposes_all_96_members_and_local_assets(self):
        response = self.client.get("/api/leagues?season_start_year=2026")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["available_seasons"], [{"season_start_year": 2026, "label": "2026/27"}])
        self.assertEqual({row["id"]: row["team_count"] for row in payload["leagues"]},
                         {"PL": 20, "PD": 20, "SA": 20, "BL1": 18, "FL1": 18})
        for league in payload["leagues"]:
            self.assertEqual(league["season_label"], "2026/27")
            self.assertTrue(league["logo_url"].startswith("/assets/leagues/"))
            self.assertTrue(all(team["crest_url"].startswith("/assets/") for team in league["teams"]))
            self.assertEqual(league["coverage"]["matches"], 0)
            self.assertFalse(league["coverage"]["has_full_schedule"])
        monaco = next(team for league in payload["leagues"] if league["id"] == "FL1" for team in league["teams"] if team["id"] == "monaco")
        self.assertEqual(monaco["country"], "摩纳哥")

    def test_unknown_season_and_league_return_explanations(self):
        response = self.client.get("/api/leagues?season_start_year=2025")
        self.assertEqual(response.status_code, 400)
        self.assertIn("赛季", response.json()["detail"])
        self.assertEqual(self.client.post("/api/leagues/unknown/sync", json={}).status_code, 400)
        self.assertEqual(self.client.get("/api/leagues?season_start_year=1800").status_code, 422)

    def test_state_lazily_materializes_only_selected_team_and_respects_spoilers(self):
        self.populate()
        response = self.client.get("/api/state?mode=live&team_id=chelsea")
        self.assertEqual(response.status_code, 200, response.text)
        state = response.json()
        self.assertEqual(len(state["matches"]), 1)
        match, = state["matches"]
        self.assertEqual(match["team_id"], "chelsea")
        self.assertEqual(match["opponent"], "阿森纳")
        self.assertIsNone(match["score"])
        self.assertTrue(match["spoiler_hidden"])
        self.assertEqual(state["evidence"], [])
        self.assertEqual(match["evidence_ids"], [])
        self.assertEqual(self.store.list("matches", "live", "arsenal"), [])
        stored, = self.store.list("matches", "live", "chelsea")
        self.assertEqual(stored["score"], "1–2")
        protected = self.client.get("/api/evidence/" + stored["evidence_ids"][0]).json()
        self.assertTrue(protected["spoiler_hidden"])
        self.assertNotIn('"home_score": 2', json.dumps(protected, ensure_ascii=False))
        self.store.save_settings({"spoiler_free": False})
        visible = self.client.get("/api/state?mode=live&team_id=chelsea").json()
        self.assertEqual(visible["matches"][0]["score"], "1–2")
        self.assertEqual(len(visible["evidence"]), 1)
        self.assertTrue(all(row["team_id"] == "chelsea" for row in visible["evidence"]))

    def test_demo_state_does_not_derive_real_league_matches(self):
        self.populate()
        response = self.client.get("/api/state?mode=demo&team_id=chelsea")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["matches"], [])
        self.assertEqual(self.store.list("matches", "live", "chelsea"), [])

    def test_unchanged_state_read_does_not_rewrite_materialized_match(self):
        self.populate()
        self.client.get("/api/state?mode=live&team_id=chelsea")
        with patch.object(self.store, "put_many", wraps=self.store.put_many) as batch:
            response = self.client.get("/api/state?mode=live&team_id=chelsea")
        self.assertEqual(response.status_code, 200)
        batch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
