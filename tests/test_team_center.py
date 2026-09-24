"""Squad identity, provenance, statistics and background maintenance regressions."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from server import team_center
from server.ingestion import IngestionError, now
from server.storage import Store
from tests.test_league_data import LEAGUE, SEASON


TEAM = {"id": "arsenal", "name": "阿森纳", "description": "球队介绍"}


def roster_payload():
    return {"season": {"year": 2026}, "timestamp": "2026-09-15T15:00:00Z",
            "team": {"id": "359", "seasonSummary": "2026-27"}, "athletes": [
        {"id": "101", "displayName": "Example Player", "citizenship": "Spain", "jersey": "8",
         "dateOfBirth": "2000-01-10T00:00Z", "age": 99, "height": 72, "weight": 160,
         "position": {"abbreviation": "M"},
         "statistics": {"splits": {"categories": [{"stats": [{"name": "appearances", "value": 2}, {"name": "totalGoals", "value": 1}]}]}}},
        {"id": "102", "displayName": "Keeper", "position": {"abbreviation": "G"}, "jersey": "1"}]}


def fixture(identity="m1", *, home=True, score=(3, 1), status="finished", season="PL-2026", day=10):
    return {"id": identity, "season_id": season, "home_team_id": "arsenal" if home else "chelsea",
            "away_team_id": "chelsea" if home else "arsenal", "home_score": score[0], "away_score": score[1],
            "status": status, "kickoff": f"2026-09-{day:02d}T18:00:00Z", "kickoff_date": f"2026-09-{day:02d}",
            "kickoff_confirmed": True, "source_url": "https://www.espn.com/soccer/match/_/gameId/1", "fetched_at": now()}


class RosterParserTests(unittest.TestCase):
    def parse(self, payload=None):
        return team_center.parse_roster(payload or roster_payload(), TEAM, LEAGUE, SEASON, "359")

    def test_fields_photo_and_chinese_fact_intro_have_provenance(self):
        parsed = self.parse()
        player = next(p for p in parsed["roster"] if p["provider_id"] == "101")
        self.assertEqual(player["nationality"], "西班牙")
        self.assertEqual(player["position"], "中场")
        self.assertEqual(player["height_cm"], 183)
        self.assertEqual(player["birth_date"], "2000-01-10")
        self.assertNotEqual(player["age"], 99)
        self.assertEqual(player["stats"]["goals"], 1)
        self.assertIsNone(player["stats"]["assists"])
        # A roster row carries facts only. A portrait is added later, by a
        # provider that has checked the player's identity and licence, so the
        # squad payload itself must never supply one.
        self.assertEqual(player["photo_url"], "")
        self.assertEqual(player["photo_credit"], "")
        self.assertIn("/id/101", player["source_url"])
        self.assertEqual(parsed["season_id"], "PL-2026")
        self.assertIn("/season/2026", parsed["source_url"])

    def test_missing_photo_is_empty_without_a_guessed_asset(self):
        player = next(p for p in self.parse()["roster"] if p["provider_id"] == "102")
        self.assertEqual(player["photo_url"], "")
        self.assertEqual(player["photo_credit"], "")
        self.assertIsNone(player["age"])
        self.assertEqual(player["bio"], "门将 · 1号")

    def test_missing_wrong_season_or_wrong_team_cannot_be_used(self):
        cases = []
        for year in (None, 2025, True, "2026"):
            payload = roster_payload()
            payload["season"] = {"year": year}
            cases.append(payload)
        payload = roster_payload()
        payload["team"]["id"] = "363"
        cases.append(payload)
        payload = roster_payload()
        payload["team"]["seasonSummary"] = "2025-26"
        cases.append(payload)
        payload = roster_payload()
        payload["league"] = {"slug": "esp.1"}
        cases.append(payload)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(IngestionError):
                self.parse(payload)

    def test_empty_or_ambiguous_roster_does_not_replace_snapshot(self):
        for athletes in ([], [roster_payload()["athletes"][0]] * 2, [{"displayName": "No ID"}], ["invalid"]):
            payload = roster_payload()
            payload["athletes"] = athletes
            with self.assertRaises(IngestionError):
                self.parse(payload)

    def test_invalid_measurements_do_not_create_misleading_facts(self):
        payload = roster_payload()
        payload["athletes"][0].update(height=float("nan"), weight=True, dateOfBirth="2099-01-01", age=-1, jersey="CAPTAIN")
        player = next(p for p in self.parse(payload)["roster"] if p["provider_id"] == "101")
        self.assertIsNone(player["height_cm"])
        self.assertIsNone(player["weight_kg"])
        self.assertIsNone(player["age"])
        self.assertEqual(player["number"], "")


class TeamCenterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "team.sqlite3")
        self.store.put("teams", TEAM)
        self.store.put("teams", {"id": "chelsea", "name": "切尔西"})
        self.store.put("leagues", LEAGUE)
        self.store.put("seasons", SEASON)
        for provider_id, team in (("359", "arsenal"), ("363", "chelsea")):
            self.store.put("league_memberships", {"id": "PL-2026-" + team, "season_id": "PL-2026", "league_id": "PL",
                           "season_start_year": 2026, "team_id": team, "provider_team_id": provider_id})
        self.store.save_settings({"spoiler_free": False})

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def save_squad(self):
        snapshot = team_center.parse_roster(roster_payload(), TEAM, LEAGUE, SEASON, "359")
        self.store.put("team_snapshots", snapshot)
        return snapshot

    def test_get_is_read_only_and_empty_data_is_explicit(self):
        view = team_center.center_view(self.store, "arsenal")
        self.assertEqual(view["roster"], [])
        self.assertEqual(view["matches"], [])
        self.assertEqual(view["stats"]["season"]["played"], 0)
        self.assertIsNone(view["stats"]["season"]["win_rate"])
        self.assertTrue(view["stale"])
        self.assertEqual(self.store.list("team_snapshots"), [])

    def test_full_standings_are_ranked_named_and_current_team_marked(self):
        self.store.put("standings", {"id": "PL-2026", "rows": [
            {"team_id": "arsenal", "rank": 2, "played": 0, "points": 0},
            {"team_id": "chelsea", "rank": 1, "played": 0, "points": 0}]})
        view = team_center.center_view(self.store, "arsenal")
        self.assertEqual([row["team_name"] for row in view["standings"]], ["切尔西", "阿森纳"])
        self.assertEqual([row["is_current"] for row in view["standings"]], [False, True])
        self.store.save_settings({"spoiler_free": True})
        hidden = team_center.center_view(self.store, "arsenal")
        self.assertEqual(hidden["standings"], [])
        self.assertTrue(hidden["standings_hidden"])

    def test_home_away_and_result_charts_come_only_from_valid_season_matches(self):
        for record in [fixture(), fixture("m2", home=False, score=(2, 2), day=11),
                       fixture("m3", home=False, score=(1, 0), day=12), fixture("next", score=(0, 0), status="scheduled", day=20),
                       fixture("old", score=(9, 0), season="PL-2025"), fixture("broken", score=(None, 0), day=13)]:
            self.store.put("league_matches", record)
        self.store.put("standings", {"id": "PL-2026", "rows": [{"team_id": "arsenal", "played": 3, "points": 2, "rank": 5}]})
        view = team_center.center_view(self.store, "arsenal")
        self.assertEqual(view["stats"]["season"]["played"], 3)
        self.assertEqual(view["stats"]["season"]["goals_for"], 5)
        self.assertEqual(view["stats"]["season"]["goals_against"], 4)
        self.assertEqual(view["stats"]["home"]["won"], 1)
        self.assertEqual(view["stats"]["away"]["lost"], 1)
        self.assertEqual(view["stats"]["recent"], ["W", "D", "L"])
        self.assertEqual(view["stats"]["standing"]["points"], 2)  # source deductions retained
        self.assertEqual(view["stats"]["season"]["points"], 4)  # separately labelled match points
        self.assertEqual(len(view["charts"]["goals_by_match"]), 3)
        self.assertTrue(view["stats"]["sample_complete"])

    def test_reduced_league_snapshot_does_not_reintroduce_old_matches(self):
        self.store.put("league_matches", fixture("included"))
        self.store.put("league_matches", fixture("excluded", score=(10, 0)))
        self.store.set_meta("league_sync_PL-2026", {"snapshot_match_ids": ["included"]})
        view = team_center.center_view(self.store, "arsenal")
        self.assertEqual(len(view["matches"]), 1)
        self.assertEqual(view["stats"]["season"]["goals_for"], 3)

    def test_spoiler_protection_removes_match_player_and_aggregate_numbers(self):
        self.save_squad()
        self.store.put("league_matches", fixture())
        self.store.save_settings({"spoiler_free": True})
        view = team_center.center_view(self.store, "arsenal")
        self.assertEqual(view["stats"], {"hidden": True})
        self.assertEqual(view["charts"], {})
        self.assertIsNone(view["matches"][0]["goals_for"])
        self.assertEqual(view["roster"][0]["stats"], {})
        self.assertFalse(team_center.center_view(self.store, "arsenal", reveal=True)["stats"]["hidden"])
        self.assertEqual(team_center.team_context_payload(self.store, "arsenal")["stats"], {"hidden": True})

    def test_rosters_are_isolated_by_season(self):
        snapshot = self.save_squad()
        snapshot.update(id="squad-PL-2025-arsenal", season_id="PL-2025")
        self.store.delete("team_snapshots", "squad-PL-2026-arsenal")
        self.store.put("team_snapshots", snapshot)
        self.assertEqual(team_center.center_view(self.store, "arsenal")["roster"], [])

    def test_snapshot_content_must_match_the_requested_team_and_season(self):
        snapshot = self.save_squad()
        snapshot["team_id"] = "chelsea"
        self.store.put("team_snapshots", snapshot)
        self.assertEqual(team_center.center_view(self.store, "arsenal")["roster"], [])

    def test_custom_team_has_an_honest_unavailable_view(self):
        self.store.put("teams", {"id": "custom", "name": "My Club"})
        view = team_center.center_view(self.store, "custom")
        self.assertEqual(view["refresh_status"], "unavailable")
        self.assertIsNone(view["season"])
        with self.assertRaises(IngestionError):
            team_center.center_view(self.store, "missing")

    def test_invalid_refresh_preserves_roster_and_original_timestamp(self):
        snapshot = self.save_squad()
        payload = roster_payload()
        payload["season"]["year"] = 2025
        with patch.object(team_center.league_data, "fetch_json", AsyncMock(return_value=payload)):
            with self.assertRaises(IngestionError):
                asyncio.run(team_center.sync_roster(self.store, "arsenal", force=True))
        self.assertEqual(self.store.get("team_snapshots", snapshot["id"]), snapshot)

    def test_fresh_roster_cache_avoids_network(self):
        self.save_squad()
        with patch.object(team_center.league_data, "fetch_json", AsyncMock()) as fetch:
            result = asyncio.run(team_center.sync_roster(self.store, "arsenal"))
        self.assertTrue(result["cached"])
        fetch.assert_not_called()

    def test_dedicated_player_photo_tool_keeps_roster_facts_and_timestamp(self):
        snapshot = self.save_squad()
        updated = __import__("copy").deepcopy(snapshot)
        updated["roster"][0].update(photo_url="https://r2.thesportsdb.com/images/media/player/cutout/a.png",
            photo_credit="TheSportsDB 社区球员资料图片", photo_source_url="https://www.thesportsdb.com/player/1",
            photo_file_page="https://www.thesportsdb.com/player/1", photo_license="第三方社区数据库转载；未声明开放许可",
            photo_match="community_name_and_nationality")
        summary = {"status": "completed", "available_count": 2, "total_count": 2, "updated_count": 1, "errors": []}
        with patch("server.team_portraits.enrich_player_photos", AsyncMock(return_value=(updated, summary)), create=True) as enrich:
            result = asyncio.run(team_center.fetch_player_photos(self.store, "arsenal", player_ids=["102"]))
        stored = self.store.get("team_snapshots", snapshot["id"])
        self.assertEqual(stored["fetched_at"], snapshot["fetched_at"])
        self.assertEqual(stored["roster"][0]["name"], snapshot["roster"][0]["name"])
        self.assertEqual(result["available_count"], 1)
        self.assertEqual(enrich.await_args.kwargs["player_ids"], ["102"])
        # Only rows that actually carry a checked portrait are returned as
        # photos, so the one enriched player is what this reports.
        self.assertEqual(len(result["photos"]), 1)

    def test_automatic_portraits_start_for_non_directory_team(self):
        self.save_squad()
        async def run():
            manager = team_center.TeamMaintenanceManager(self.store)
            with patch.object(team_center, "sync_portraits", AsyncMock(return_value={"status": "missing"})) as sync:
                self.assertTrue(manager._start_portraits("arsenal"))
                await asyncio.gather(*manager.portrait_tasks.values())
                sync.assert_awaited_once()
        asyncio.run(run())

    def test_dedicated_photos_cannot_overwrite_a_newer_roster(self):
        snapshot = self.save_squad()
        newer = {**snapshot, "fetched_at": "2099-01-01T00:00:00Z"}
        async def enrich(*args, **kwargs):
            self.store.put("team_snapshots", newer)
            return snapshot, {"status": "completed", "errors": []}
        with patch("server.team_portraits.enrich_player_photos", enrich, create=True):
            result = asyncio.run(team_center.fetch_player_photos(self.store, "arsenal"))
        self.assertEqual(self.store.get("team_snapshots", snapshot["id"]), newer)
        self.assertEqual(result["status"], "interrupted")

    def test_refresh_failure_preserves_data_and_is_rate_limited(self):
        snapshot = self.save_squad()
        with patch.object(team_center.league_data, "sync_standings", AsyncMock(return_value={"sync_status": "failed", "errors": ["积分暂不可用"]})), \
             patch.object(team_center.league_data, "sync_team_recent", AsyncMock(return_value={"sync_status": "failed", "errors": ["球队赛程暂不可用"]})), \
             patch.object(team_center, "sync_roster", AsyncMock(side_effect=IngestionError("阵容暂不可用"))) as fetch:
            async def run():
                first = await team_center.maintain_team(self.store, "arsenal", force=True, parts=["roster", "schedule", "standings"])
                second = await team_center.maintain_team(self.store, "arsenal", force=True, parts=["roster", "schedule", "standings"])
                return first, second
            first, second = asyncio.run(run())
        self.assertEqual(first["status"], "failed")
        self.assertTrue(second["cached"])
        self.assertEqual(fetch.await_count, 1)
        self.assertEqual(self.store.get("team_snapshots", snapshot["id"]), snapshot)

    def test_background_manager_deduplicates_and_closes_cleanly(self):
        async def run():
            manager = team_center.TeamMaintenanceManager(self.store)
            gate = asyncio.Event()
            async def maintenance(*args, **kwargs):
                await gate.wait()
            with patch.object(team_center, "maintain_team", side_effect=maintenance):
                first = manager.start("arsenal")
                await asyncio.sleep(0)
                second = manager.start("arsenal")
                self.assertTrue(first["started"])
                self.assertFalse(second["started"])
                await manager.close()
            self.assertTrue(all(t.done() for t in manager.tasks.values()))
        asyncio.run(run())
        self.assertEqual(team_center.center_view(self.store, "arsenal")["refresh_status"], "interrupted")

    def test_manager_close_before_coroutine_starts_resets_running_marker(self):
        async def run():
            manager = team_center.TeamMaintenanceManager(self.store)
            manager.start("arsenal")
            await manager.close()
        asyncio.run(run())
        self.assertEqual(team_center.center_view(self.store, "arsenal")["refresh_status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
