"""Panel workflow: six leaves, reason codes, partial saves and chart samples."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from server import skill_catalog, team_center, team_panel, tool_registry
from server.agent_tools import TOOL_DEFINITIONS
from server.decisions import begin
from server.ingestion import now
from server.storage import Store
from server.workflows import WorkflowManager
from tests.test_league_data import LEAGUE, SEASON
from tests.test_team_center import TEAM, fixture, roster_payload


class PanelContractTests(unittest.TestCase):
    def test_reason_codes_match_the_panel_table(self):
        codes = skill_catalog.PANEL_REASON_CODES
        self.assertEqual(set(codes), {
            "MISSING", "STALE", "SOURCE_UNAVAILABLE", "RATE_LIMITED", "MODEL_FAILED", "NAMESAKE_BLOCKED",
            "CREST_MISSING", "INCOMPLETE_SCHEDULE", "SAMPLE_INSUFFICIENT", "SCOPE_DENIED", "INTERRUPTED",
        })
        self.assertTrue(codes["MODEL_FAILED"])
        for code in ("CREST_MISSING", "NAMESAKE_BLOCKED", "SAMPLE_INSUFFICIENT", "SCOPE_DENIED", "INTERRUPTED"):
            self.assertFalse(codes[code], code)
        self.assertTrue(codes["INCOMPLETE_SCHEDULE"])
        self.assertEqual(skill_catalog.PANEL_LEAVES, tuple(skill_catalog.TOPICS["team"]))

    def test_child_tools_stay_inside_the_parent_and_charts_cannot_maintain(self):
        from server.agent_delegation import DELEGATION_TOOLS
        parent = set()
        for tool in tool_registry.select([*TOOL_DEFINITIONS, *DELEGATION_TOOLS], tool_registry.TEAM_WORK):
            parent.add(tool.get("name") or tool["function"]["name"])
        for leaf in skill_catalog.PANEL_LEAVES:
            names = team_panel.child_tools(leaf)
            self.assertTrue(set(names) <= parent, leaf)
            self.assertNotIn("review_maintenance", names)
        self.assertIn("fetch_player_photos", team_panel.child_tools("squad.images"))
        charts = team_panel.child_tools("stats.charts")
        self.assertNotIn("maintain_team_data", charts)
        self.assertNotIn("fetch_player_photos", charts)
        names = team_panel.child_tools("squad.names")
        self.assertEqual(names, ["get_team_data", "use_skill", "submit_answer"])
        self.assertNotIn("maintain_team_data", names)
        self.assertNotIn("fetch_player_photos", names)
        declared = tool_registry.topic_declared("team", "stats.charts")["capabilities"]
        self.assertTrue(set(declared) <= set(tool_registry.declared("team")))


class PanelDataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "panel.sqlite3")
        self.store.put("teams", {**TEAM})
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

    def save_roster(self, *, photos=True, namesake=False):
        payload = roster_payload()
        if photos or namesake:
            payload["athletes"][0]["headshot"] = {"href": "https://a.espncdn.com/i/headshots/soccer/players/full/101.png"}
            if not namesake:
                payload["athletes"][1]["headshot"] = {"href": "https://a.espncdn.com/i/headshots/soccer/players/full/102.png"}
        snapshot = team_center.parse_roster(payload, TEAM, LEAGUE, SEASON, "359")
        if namesake:
            player = next(row for row in snapshot["roster"] if row["provider_id"] == "102")
            player.update(photo_url="", namesake_blocked=True, retryable=False,
                          reason_code="community_namesake_blocked", photo_reason="来源只返回了同名他人。")
        self.store.put("team_snapshots", snapshot)
        return snapshot

    def save_profile(self):
        self.store.put("teams", {**self.store.get("teams", "arsenal"), "founded": 1886, "stadium": "酋长球场",
                                 "nickname": "枪手", "crest_url": "/assets/clubs/arsenal.png",
                                 "crest_source_url": "https://example.test/crest", "crest_attribution": "Club",
                                 "profile_updated_at": now()})

    def save_table(self, *, played=2):
        self.store.put("standings", {"id": "PL-2026", "fetched_at": now(), "source_url": "https://www.espn.com/soccer/standings/_/league/eng.1",
                                     "rows": [{"team_id": "arsenal", "rank": 1, "played": played, "points": 4},
                                              {"team_id": "chelsea", "rank": 2, "played": played, "points": 1}]})

    def save_schedule_meta(self, *, at=None):
        self.store.set_meta("league_sync_" + SEASON["id"], {"fixtures_at": at or now(), "fixtures_status": "completed"})

    def fresh_club(self):
        self.save_profile()
        snapshot = self.save_roster()
        # Readings the shipped table already holds, so the names leaf is full
        # without a model call. The source spelling stays the identity.
        by_id = {row["provider_id"]: row for row in snapshot["roster"]}
        by_id["101"]["name"] = "Bukayo Saka"
        by_id["102"]["name"] = "Martin Odegaard"
        self.store.put("team_snapshots", snapshot)
        self.store.put("league_matches", fixture())
        self.store.put("league_matches", fixture("m2", home=False, score=(1, 1), day=11))
        self.save_table()
        self.save_schedule_meta()

    def test_a_full_fresh_club_does_not_fetch_again(self):
        self.fresh_club()
        assessed = team_panel.assess(self.store, "arsenal")
        self.assertEqual(assessed["actionable_count"], 0)
        self.assertTrue(assessed["early_stop"])
        called = []

        async def unused(*args, **kwargs):
            called.append(kwargs)
            return {"components": {}}

        async def run():
            manager = WorkflowManager(self.store)
            try:
                with patch("server.team_center.maintain_team", new=unused), \
                     patch("server.team_center.sync_portraits", new=unused):
                    self.assertTrue(manager.start("team", "arsenal")["started"])
                    await asyncio.wait_for(manager.tasks[("team", "arsenal")].task, 2)
                state = manager.status("team", "arsenal")
                self.assertEqual(called, [])
                self.assertEqual(state["status"], "completed")
                self.assertEqual(state["progress"]["remaining"], 0)
                self.assertEqual(state["progress"]["failed"], 0)
                self.assertEqual({row["id"] for row in state["leaves"]}, set(skill_catalog.PANEL_LEAVES))
                self.assertTrue(all(row["status"] == "filled" for row in state["leaves"]))
                self.assertTrue(state["panel"]["early_stop"])
            finally:
                await manager.close()

        asyncio.run(run())

    def test_namesake_photos_are_filled_and_not_retried(self):
        self.fresh_club()
        self.save_roster(namesake=True)
        leaf = next(row for row in team_panel.assess(self.store, "arsenal")["leaves"] if row["id"] == "squad.images")
        self.assertEqual(leaf["status"], "filled")
        self.assertEqual(leaf["reason_code"], "NAMESAKE_BLOCKED")
        self.assertFalse(leaf["retryable"])
        called = []

        async def unused(*args, **kwargs):
            called.append("called")
            return {"status": "completed", "components": {}}

        async def run():
            manager = WorkflowManager(self.store)
            try:
                with patch("server.team_center.sync_portraits", new=unused):
                    manager.start("team", "arsenal")
                    await asyncio.wait_for(manager.tasks[("team", "arsenal")].task, 2)
                self.assertEqual(called, [])
            finally:
                await manager.close()

        asyncio.run(run())

    def test_missing_identity_is_a_gap_and_empty_position_is_not(self):
        snapshot = self.save_roster()
        snapshot["roster"].append({"name": "No Key", "position": "", "number": ""})
        snapshot["roster"].append({"provider_id": "303", "id": "espn-player-303", "name": "Zzq Qqx",
                                    "position": "", "number": ""})
        self.store.put("team_snapshots", snapshot)
        leaf = next(row for row in team_panel.assess(self.store, "arsenal")["leaves"] if row["id"] == "squad.info")
        self.assertEqual(leaf["status"], "gap")
        self.assertTrue(leaf["actionable"])
        self.assertEqual(leaf["identity_gaps"][0]["name"], "No Key")
        self.assertTrue(any(row["name"] == "Zzq Qqx" and row["field"] == "name_zh" for row in leaf["name_gaps"]))
        self.assertNotIn("位置", json.dumps(leaf["gaps"], ensure_ascii=False))
        self.assertNotIn("号码", json.dumps(leaf["gaps"], ensure_ascii=False))

    def test_chinese_names_follow_the_roster_and_do_not_look_names_up(self):
        waiting = team_panel._names_leaf({"roster": [], "components": {}})
        self.assertFalse(waiting["actionable"])
        self.assertEqual(waiting["depends_on"], ["squad.info"])
        self.assertNotIn("maintain_team_data", waiting["tools"])

        blocked = team_panel._names_leaf({"roster": [{"name": "Zzq Qqx"}], "components": {"roster": {"status": "fresh"}}})
        self.assertFalse(blocked["actionable"])
        self.assertEqual(blocked["depends_on"], ["squad.info"])

        pending = team_panel._names_leaf({"roster": [
            {"provider_id": "9", "name": "Zzq Qqx"},
        ], "components": {"roster": {"status": "fresh"}}})
        self.assertEqual(pending["status"], "gap")
        self.assertTrue(pending["actionable"])
        self.assertTrue(pending["retryable"])
        self.assertEqual(pending["depends_on"], [])
        self.assertTrue(pending["parallel"])
        self.assertEqual(pending["player_gaps"][0]["provider_id"], "9")

        terminal = team_panel._names_leaf({"roster": [
            {"provider_id": "9", "name": ""},
            {"provider_id": "8", "name": "Kept", "name_zh": "可疑译名"},
        ], "components": {"roster": {"status": "fresh"}}})
        self.assertEqual(terminal["status"], "filled")
        self.assertFalse(terminal["actionable"])
        self.assertFalse(terminal["retryable"])
        self.assertEqual(terminal["reason_code"], "MISSING")
        self.assertEqual(terminal["player_gaps"], [{
            "provider_id": "9", "reason_code": "MISSING", "retryable": False, "detail": "没有英文名可译。",
        }])

        failed = team_panel._names_leaf({"roster": [
            {"provider_id": "9", "name": "Zzq Qqx", "name_zh_reason_code": "MODEL_FAILED"},
        ], "components": {"roster": {"status": "fresh"}}})
        self.assertEqual(failed["reason_code"], "MODEL_FAILED")
        self.assertTrue(failed["retryable"])
        self.assertTrue(failed["actionable"])

        self.save_roster(photos=False)
        assessed = team_panel.assess(self.store, "arsenal")
        self.assertEqual([row["id"] for row in assessed["leaves"]], list(skill_catalog.PANEL_LEAVES))
        names = next(row for row in assessed["leaves"] if row["id"] == "squad.names")
        images = next(row for row in assessed["leaves"] if row["id"] == "squad.images")
        self.assertTrue(names["actionable"])
        self.assertTrue(images["actionable"])
        self.assertEqual(names["depends_on"], [])
        self.assertTrue(names["parallel"])
        self.assertEqual(names["maintain_parts"], [])

    def test_name_gaps_do_not_keep_a_valid_roster_open(self):
        snapshot = self.save_roster()
        for row in snapshot["roster"]:
            row["name"] = "Zzq Qqx " + row["provider_id"]
        self.store.put("team_snapshots", snapshot)
        leaf = next(row for row in team_panel.assess(self.store, "arsenal")["leaves"] if row["id"] == "squad.info")
        self.assertTrue(leaf["name_gaps"])
        self.assertFalse(leaf["actionable"])
        self.assertEqual(leaf["status"], "filled")

    def test_one_match_of_two_is_an_incomplete_schedule(self):
        self.save_profile()
        self.save_roster()
        self.store.put("league_matches", fixture())
        self.save_table(played=1)
        self.save_schedule_meta()
        leaf = next(row for row in team_panel.assess(self.store, "arsenal")["leaves"] if row["id"] == "schedule.standings")
        self.assertEqual(leaf["reason_code"], "INCOMPLETE_SCHEDULE")
        self.assertTrue(leaf["retryable"])
        self.assertNotEqual(leaf["status"], "filled")
        self.assertEqual(leaf["schedule"]["matches"], 1)
        self.assertEqual(leaf["schedule"]["expected_matches"], 2)

    def test_a_rate_limit_on_standings_uses_that_reason(self):
        self.fresh_club()
        self.store.set_meta("league_standings_" + SEASON["id"], {"status": "failed", "errors": ["来源限流 HTTP 429"]})
        leaf = next(row for row in team_panel.assess(self.store, "arsenal")["leaves"] if row["id"] == "schedule.standings")
        self.assertEqual(leaf["reason_code"], "RATE_LIMITED")
        self.assertTrue(leaf["retryable"])

    def test_charts_use_the_finished_league_sample_and_do_not_write(self):
        self.fresh_club()
        self.store.put("league_matches", fixture("m3", home=False, score=(1, 0), day=12))
        self.save_table(played=3)
        before = json.dumps(self.store.get_meta("league_sync_" + SEASON["id"]))
        view = team_center.center_view(self.store, "arsenal", reveal=True)
        blocks = view["chart_blocks"]
        again = team_panel.chart_blocks(json.loads(json.dumps(view)))
        self.assertEqual(self.store.get_meta("league_sync_" + SEASON["id"])["fixtures_at"], json.loads(before)["fixtures_at"])
        self.assertEqual(blocks["status"], "ready")
        self.assertIsNone(blocks["reason_code"])
        self.assertEqual(blocks["sample"]["from"], "2026-09-10")
        self.assertEqual(blocks["sample"]["to"], "2026-09-12")
        self.assertEqual(blocks["sample"]["played"], 3)
        self.assertEqual(blocks["results"], {"won": 1, "drawn": 1, "lost": 1, "played": 3})
        self.assertEqual(blocks["goals"], {"goals_for": 4, "goals_against": 3, "played": 3})
        self.assertEqual(blocks["home_away"]["home"]["played"], 1)
        self.assertEqual(blocks["home_away"]["away"]["played"], 2)
        self.assertTrue(blocks["sample"]["sources"])
        self.assertEqual(again["results"], blocks["results"])

    def test_a_scheduled_sample_is_empty_rather_than_zeros(self):
        blocks = team_panel.chart_blocks({"stats": {"hidden": False}, "league": {"id": "PL", "name": "英超"},
                                          "season": {"id": "PL-2026"}, "matches": [
                                              {"status": "scheduled", "competition_id": "PL", "goals_for": 0,
                                               "goals_against": 0, "venue": "home", "kickoff_date": "2026-09-20"}]})
        self.assertEqual(blocks["reason_code"], "SAMPLE_INSUFFICIENT")
        self.assertFalse(blocks["retryable"])
        self.assertIsNone(blocks["results"])
        self.assertIsNone(blocks["goals"])
        self.assertIsNone(blocks["home_away"])
        self.assertEqual(blocks["sample"]["played"], 0)

    def test_a_hidden_sample_does_not_leak_scores(self):
        blocks = team_panel.chart_blocks({"stats": {"hidden": True}, "matches": [
            {"status": "finished", "competition_id": "PL", "goals_for": 9, "goals_against": 4,
             "venue": "home", "kickoff_date": "2026-09-10"}]})
        self.assertEqual(blocks["reason_code"], "SCOPE_DENIED")
        self.assertFalse(blocks["retryable"])
        self.assertIsNone(blocks["sample"])
        self.assertNotIn("9", json.dumps(blocks))

    def test_a_failed_schedule_refresh_keeps_the_old_snapshot_and_its_chart(self):
        self.fresh_club()
        snapshot = self.store.get("team_snapshots", "squad-PL-2026-arsenal")
        fetched_at = snapshot["fetched_at"]

        async def fail_schedule(store, team_id, **kwargs):
            self.assertIn("schedule", kwargs.get("parts") or [])
            return {"components": {"schedule": {"status": "failed", "errors": ["来源超时"]}}, "errors": ["来源超时"]}

        async def run():
            self.save_schedule_meta(at="2000-01-01T00:00:00+00:00")
            manager = WorkflowManager(self.store)
            try:
                with patch("server.team_center.maintain_team", new=fail_schedule):
                    manager.start("team", "arsenal")
                    await asyncio.wait_for(manager.tasks[("team", "arsenal")].task, 2)
                state = manager.status("team", "arsenal")
                leaf = next(row for row in state["leaves"] if row["id"] == "schedule.standings")
                self.assertEqual(leaf["status"], "failed")
                self.assertEqual(leaf["reason_code"], "SOURCE_UNAVAILABLE")
                self.assertTrue(leaf["retryable"])
                self.assertEqual(self.store.get("team_snapshots", snapshot["id"])["fetched_at"], fetched_at)
                blocks = team_panel.chart_blocks(team_center.center_view(self.store, "arsenal", reveal=True))
                self.assertEqual(blocks["results"]["played"], 2)
                self.assertNotIn("INCOMPLETE_SCHEDULE", state.get("last_error") or "")
                self.assertNotIn("SAMPLE_INSUFFICIENT", state.get("last_error") or "")
            finally:
                await manager.close()

        asyncio.run(run())

    def test_cancelling_a_refresh_marks_running_leaves_interrupted(self):
        snapshot = self.save_roster()
        fetched_at = snapshot["fetched_at"]
        entered = asyncio.Event()

        async def hang(store, team_id, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        async def run():
            manager = WorkflowManager(self.store)
            try:
                with patch("server.team_center.maintain_team", new=hang):
                    manager.start("team", "arsenal")
                    await asyncio.wait_for(entered.wait(), 2)
                    stopped = await manager.stop("team", "arsenal")
                self.assertEqual(stopped["status"], "stopped")
                interrupted = [row for row in stopped["leaves"] if row["reason_code"] == "INTERRUPTED"]
                self.assertTrue(interrupted)
                self.assertTrue(all(row["retryable"] is False and row["status"] == "interrupted" for row in interrupted))
                self.assertEqual(self.store.get("team_snapshots", snapshot["id"])["fetched_at"], fetched_at)
            finally:
                await manager.close()

        asyncio.run(run())

    def test_a_configured_team_model_does_not_replace_the_panel(self):
        from server import agent
        self.store.put("model_profiles", {"id": "alpha", "name": "Alpha", "base_url": "https://alpha.example/v1",
                                          "protocol": "chat_completions",
                                          "models": [{"id": "one", "name": "One"}]})
        self.store.set_meta("model_roles", {"team": {"provider_id": "alpha", "model": "one"}})
        called = []

        async def provider(*args, **kwargs):
            called.append(True)
            return {"role": "assistant", "content": ""}

        async def maintain(*args, **kwargs):
            return {"status": "completed", "components": {}}

        async def run():
            manager = WorkflowManager(self.store)
            self.assertTrue(manager._team_agent_configured())
            try:
                with patch.object(agent, "request_completion", new=provider), \
                     patch("server.team_center.maintain_team", new=maintain), \
                     patch("server.team_center.sync_portraits", new=maintain):
                    manager.start("team", "arsenal")
                    await asyncio.wait_for(manager.tasks[("team", "arsenal")].task, 2)
                state = manager.status("team", "arsenal")
                self.assertEqual(called, [])
                self.assertEqual([row["id"] for row in state["leaves"]], list(skill_catalog.PANEL_LEAVES))
                self.assertFalse(any(row.get("skill") == "team_refresh" for row in self.store.list("runs")))
            finally:
                await manager.close()

        asyncio.run(run())

    def test_team_refresh_decisions_stay_a_separate_kind(self):
        view = team_center.center_view(self.store, "arsenal", reveal=True)
        identity = begin(self.store, "arsenal", view, parts=("roster",), trigger="manual", reason="核对阵容")
        self.assertEqual(self.store.get("decisions", identity)["kind"], "team_refresh")


class PanelRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_charts_and_workflow_status_expose_the_bar_fields(self):
        from server.main import create_app
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory) / "panel.sqlite3")
            try:
                app.state.store.save_settings({"spoiler_free": True})
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                    hidden = (await client.get("/api/teams/arsenal/charts")).json()
                    revealed = (await client.get("/api/teams/arsenal/charts?reveal=true")).json()
                    panel = (await client.get("/api/teams/arsenal/panel")).json()
                    workflow = (await client.get("/api/workflows/team/arsenal")).json()
                self.assertEqual(hidden["reason_code"], "SCOPE_DENIED")
                self.assertIsNone(hidden["results"])
                self.assertEqual(revealed["reason_code"], "SAMPLE_INSUFFICIENT")
                self.assertIsNone(revealed["results"])
                self.assertIsNone(revealed["goals"])
                self.assertIsNone(revealed["home_away"])
                self.assertEqual(panel["workflow"], "team")
                self.assertEqual([row["id"] for row in panel["skills"]], list(skill_catalog.PANEL_LEAVES))
                for key in ("completed", "failed", "remaining", "settled"):
                    self.assertIn(key, workflow["progress"])
            finally:
                await app.state.workflows.close()
                app.state.store.close()


if __name__ == "__main__":
    unittest.main()
