"""A real reduced ESPN team schedule may supplement, never replace, a season."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from server import league_data, team_center
from server.ingestion import IngestionError, now
from server.storage import Store


LEAGUE = {"id": "PD", "name": "西甲", "provider_code": "esp.1", "provider_league_uid": "s:600~l:740", "expected_team_count": 2}
SEASON = {"id": "PD-2026", "league_id": "PD", "season_start_year": 2026, "label": "2026/27"}
MAPPING = {"83": "barcelona", "1538": "levante"}


def schedule():
    return json.loads((Path(__file__).parent / "fixtures/espn-barcelona-schedule-sample.json").read_text(encoding="utf-8"))


class TeamScheduleParserTests(unittest.TestCase):
    def parse(self, payload=None):
        return league_data.parse_team_schedule(payload or schedule(), LEAGUE, SEASON, MAPPING, "83")

    def test_real_nested_score_values_and_finished_status_are_parsed(self):
        result = self.parse()
        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertEqual(row["provider_match_id"], "401882883")
        self.assertEqual((row["home_team_id"], row["away_team_id"]), ("levante", "barcelona"))
        self.assertEqual((row["home_score"], row["away_score"]), (2, 4))
        self.assertEqual(row["status"], "finished")
        self.assertTrue(row["kickoff_confirmed"])

    def test_response_team_and_both_season_echoes_must_match(self):
        cases = []
        for key in ("season", "requestedSeason"):
            for value in (None, 2025, True, "2026"):
                payload = schedule()
                payload[key] = {"year": value}
                cases.append(payload)
        payload = schedule()
        payload["team"]["id"] = "359"
        cases.append(payload)
        payload = schedule()
        payload["team"]["seasonSummary"] = "2025-26"
        cases.append(payload)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(IngestionError):
                self.parse(payload)

    def test_other_competitions_are_filtered_and_unknown_identity_is_rejected(self):
        payload = schedule()
        cup = copy.deepcopy(payload["events"][0])
        cup["id"] = "999999"
        cup["league"] = {"slug": "uefa.champions", "id": "775"}
        cup["competitions"][0]["competitors"][0]["id"] = "foreign-club"
        payload["events"].append(cup)
        self.assertEqual(len(self.parse(payload)), 1)
        for league in ({}, {"slug": "esp.1", "id": "700"}):
            payload = schedule()
            payload["events"][0]["league"] = league
            with self.assertRaises(IngestionError):
                self.parse(payload)

    def test_event_season_and_selected_team_participation_are_required(self):
        payload = schedule()
        payload["events"][0]["season"]["year"] = 2025
        with self.assertRaises(IngestionError):
            self.parse(payload)
        payload = schedule()
        payload["events"][0]["competitions"][0]["competitors"][1]["id"] = "359"
        with self.assertRaises(IngestionError):
            self.parse(payload)


class TeamScheduleSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "schedule.sqlite3")
        for provider_id, identity in MAPPING.items():
            self.store.put("teams", {"id": identity, "name": identity})
            self.store.put("league_memberships", {"id": "PD-2026-" + identity, "season_id": "PD-2026", "league_id": "PD",
                "season_start_year": 2026, "team_id": identity, "provider_team_id": provider_id})
        self.store.put("leagues", LEAGUE)
        self.store.put("seasons", SEASON)
        self.store.save_settings({"spoiler_free": False})

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def future(self):
        row = league_data.parse_team_schedule(schedule(), LEAGUE, SEASON, MAPPING, "83")[0]
        return {**row, "id": "espn-PD-2026-future", "provider_match_id": "9999999", "status": "scheduled",
                "kickoff": "2027-01-01T20:00:00+00:00", "kickoff_date": "2027-01-01", "home_score": None, "away_score": None}

    async def test_partial_team_feed_preserves_future_snapshot_ids_and_full_league_age(self):
        future = self.future()
        self.store.put("league_matches", future)
        original = {"last_sync": "2026-09-12T12:00:00+00:00", "fixtures_at": "2026-09-12T12:00:00+00:00",
                    "sync_status": "partial", "fixtures_status": "failed", "errors": ["赛程来源HTTP400"], "snapshot_match_ids": [future["id"]]}
        self.store.set_meta("league_sync_PD-2026", original)
        self.store.put("standings", {"id": "PD-2026", "rows": [{"team_id": "barcelona", "played": 1, "points": 3, "rank": 1}]})
        with patch.object(league_data, "fetch_json", new=AsyncMock(return_value=schedule())) as fetch:
            result = await league_data.sync_team_recent(self.store, "barcelona")
        self.assertEqual(result["sync_status"], "completed")
        self.assertEqual(self.store.get("league_matches", future["id"]), future)
        current = self.store.get_meta("league_sync_PD-2026")
        self.assertEqual(len(current["snapshot_match_ids"]), 2)
        for key in ("last_sync", "fixtures_at", "sync_status", "fixtures_status", "errors"):
            self.assertEqual(current[key], original[key])
        self.assertEqual(fetch.await_args_list[0].args[0], "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/teams/83/schedule?season=2026")
        self.assertTrue(fetch.await_args_list[1].args[0].endswith("&fixture=true"))
        view = team_center.center_view(self.store, "barcelona")
        self.assertEqual(view["stats"]["season"]["played"], 1)
        self.assertEqual(view["stats"]["season"]["goals_for"], 4)
        self.assertTrue(view["stats"]["sample_complete"])
        self.assertEqual(len(view["charts"]["goals_by_match"]), 1)
        self.assertTrue(view["team_results_updated_at"])
        self.assertEqual(view["fixtures_updated_at"], original["fixtures_at"])

    async def test_bad_year_team_and_regressed_finished_status_never_overwrite_records(self):
        existing = league_data.parse_team_schedule(schedule(), LEAGUE, SEASON, MAPPING, "83")[0]
        self.store.put("league_matches", existing)
        cases = []
        wrong_year = schedule()
        wrong_year["season"]["year"] = 2025
        cases.append(wrong_year)
        wrong_team = schedule()
        wrong_team["team"]["id"] = "359"
        cases.append(wrong_team)
        regression = schedule()
        regression["events"][0]["competitions"][0]["status"]["type"] = {"name": "STATUS_SCHEDULED", "state": "pre", "completed": False}
        cases.append(regression)
        for payload in cases:
            self.store.set_meta("team_schedule_PD-2026_barcelona", {})
            with patch.object(league_data, "fetch_json", new=AsyncMock(return_value=payload)):
                result = await league_data.sync_team_recent(self.store, "barcelona", force=True)
            self.assertEqual(result["sync_status"], "failed")
            self.assertEqual(self.store.get("league_matches", existing["id"]), existing)

    async def test_duplicate_requests_share_one_request_pair_without_other_teams(self):
        with patch.object(league_data, "fetch_json", new=AsyncMock(return_value=schedule())) as fetch:
            first, second = await asyncio.gather(league_data.sync_team_recent(self.store, "barcelona"), league_data.sync_team_recent(self.store, "barcelona"))
        self.assertEqual(fetch.await_count, 2)
        self.assertTrue(second["cached"])
        self.assertEqual(first["team_id"], "barcelona")
        self.assertEqual(self.store.get_meta("team_schedule_PD-2026_levante"), None)

    async def test_maintenance_calls_fallback_after_scoreboard_failure(self):
        async def unavailable(*args, **kwargs):
            result = {"sync_status": "partial", "fixtures_status": "failed", "errors": ["赛程来源HTTP400"], "last_sync": now()}
            self.store.set_meta("league_sync_PD-2026", result)
            return result
        with patch.object(league_data, "sync_league", side_effect=unavailable), \
             patch.object(team_center, "sync_roster", new=AsyncMock(return_value={"players": 0})), \
             patch.object(league_data, "fetch_json", new=AsyncMock(return_value=schedule())) as fetch:
            result = await team_center.maintain_team(self.store, "barcelona", force=True)
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(result["status"], "partial")  # full-league error stays honest
        self.assertEqual(team_center.center_view(self.store, "barcelona")["stats"]["season"]["played"], 1)

    async def test_reduced_previous_snapshot_does_not_resurrect_excluded_old_records(self):
        hidden = self.future()
        self.store.put("league_matches", hidden)
        self.store.set_meta("league_sync_PD-2026", {"snapshot_match_ids": []})
        with patch.object(league_data, "fetch_json", new=AsyncMock(return_value=schedule())):
            await league_data.sync_team_recent(self.store, "barcelona")
        self.assertNotIn(hidden["id"], self.store.get_meta("league_sync_PD-2026")["snapshot_match_ids"])
        self.assertIsNotNone(self.store.get("league_matches", hidden["id"]))

    async def test_missing_future_data_is_added_without_removing_finished_games(self):
        future = schedule()
        future["events"][0]["id"] = "9999999"
        future["events"][0]["date"] = "2027-01-01T20:00:00Z"
        future["events"][0]["competitions"][0]["date"] = "2027-01-01T20:00:00Z"
        future["events"][0]["competitions"][0]["status"]["type"] = {"name": "STATUS_SCHEDULED", "state": "pre", "completed": False}
        with patch.object(league_data, "fetch_json", new=AsyncMock(side_effect=[schedule(), future])):
            result = await league_data.sync_team_recent(self.store, "barcelona")
        self.assertEqual(result["match_count"], 2)
        view = team_center.center_view(self.store, "barcelona")
        self.assertEqual(view["stats"]["season"]["played"], 1)
        self.assertEqual(sum(item["status"] == "scheduled" for item in view["matches"]), 1)
        self.assertTrue(view["team_schedule_updated_at"])

    async def test_one_endpoint_failure_keeps_previous_future_data_and_updates_results(self):
        future = self.future()
        self.store.put("league_matches", future)
        with patch.object(league_data, "fetch_json", new=AsyncMock(side_effect=[schedule(), IngestionError("未来赛程暂不可用")])):
            result = await league_data.sync_team_recent(self.store, "barcelona")
        self.assertEqual(result["sync_status"], "partial")
        self.assertEqual(self.store.get("league_matches", future["id"]), future)
        self.assertTrue(result["results_updated_at"])
        self.assertIsNone(result["upcoming_updated_at"])


if __name__ == "__main__":
    unittest.main()
