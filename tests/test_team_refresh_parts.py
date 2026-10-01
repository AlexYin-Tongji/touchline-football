"""Independent team refresh, bounded fallback and truthful source coverage."""
import asyncio
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from server import collection, decisions, league_data, team_center, team_portraits
from server.collection_scheduler import CollectionScheduler
from server.ingestion import IngestionError, now
from server.storage import Store
from tests.test_league_data import LEAGUE, SEASON, MAPPING, scoreboard, standings
from tests.test_team_center import TEAM, roster_payload


OLD = "2020-01-01T00:00:00+00:00"


def schedule_payload(*, future=False):
    payload = scoreboard(state="scheduled" if future else "finished", match_id="820002" if future else "820001",
                         home_id="363" if future else "359", away_id="359" if future else "363")
    payload.update(season={"year": 2026}, requestedSeason={"year": 2026}, team={"id": "359"})
    payload["events"][0]["league"] = {"slug": "eng.1", "id": "700"}
    return payload


class IndependentRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "team.sqlite3")
        self.store.put("leagues", LEAGUE)
        self.store.put("seasons", SEASON)
        for provider_id, team_id in MAPPING.items():
            self.store.put("teams", TEAM if team_id == "arsenal" else {"id": team_id, "name": team_id})
            self.store.put("league_memberships", {"id": SEASON["id"] + team_id, "season_id": SEASON["id"],
                "season_start_year": 2026, "league_id": "PL", "team_id": team_id, "provider_team_id": provider_id})
        self.store.save_settings({"spoiler_free": False})

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def source(self, url):
        if "/roster?" in url:
            return roster_payload()
        if "/standings?" in url:
            return standings()
        if "/schedule?" in url:
            return schedule_payload(future="fixture=true" in url)
        raise AssertionError("A single-team refresh must not fetch a whole-league scoreboard: " + url)

    async def test_manual_maintenance_records_its_reason_and_observed_roster(self):
        from server.decisions import history
        with patch.object(league_data, "fetch_json", side_effect=self.source):
            await team_center.maintain_team(self.store, "arsenal", parts=["roster"])
        rows = history(self.store, "arsenal")["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[0]["after"]["roster"]["count"], 2)
        self.assertTrue(rows[0]["changes"][0]["confirmed"])

    async def test_planned_maintenance_joins_the_recorded_decision(self):
        from server.decisions import history
        manager = team_center.TeamMaintenanceManager(self.store)
        try:
            with patch.object(league_data, "fetch_json", side_effect=self.source):
                result = await manager.review_team("arsenal", trigger="automatic")
                outcome = await manager.wait_for_team("arsenal")
            self.assertEqual(outcome["status"], "completed")
            rows = history(self.store, "arsenal")["items"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], result["decision_id"])
            self.assertEqual(rows[0]["trigger"], "automatic")
            self.assertTrue(rows[0]["plan"]["needs"])
            import json
            json.dumps(result, allow_nan=False)
        finally:
            await manager.close()

    async def test_cancellation_before_background_task_starts_finishes_its_decision(self):
        from server.decisions import history
        manager = team_center.TeamMaintenanceManager(self.store)
        await manager.review_team("arsenal", trigger="automatic")
        await manager.close()
        rows = history(self.store, "arsenal")["items"]
        self.assertEqual(rows[0]["status"], "interrupted")

    async def test_core_roster_is_saved_without_waiting_for_optional_portraits(self):
        release = asyncio.Event()
        async def slow_portrait(store, snapshot):
            await release.wait()
            return snapshot
        with patch.object(league_data, "fetch_json", side_effect=self.source), \
             patch.object(team_portraits, "enrich_portraits", side_effect=slow_portrait):
            task = asyncio.create_task(team_center.sync_roster(self.store, "arsenal"))
            try:
                done, _ = await asyncio.wait({task}, timeout=.2)
                self.assertIn(task, done, "optional photos must not hold the roster response")
                self.assertEqual(len(team_center.center_view(self.store, "arsenal")["roster"]), 2)
            finally:
                release.set()
                await task

    async def test_roster_becomes_visible_while_schedule_is_still_running(self):
        waiting, release = asyncio.Event(), asyncio.Event()
        async def source(url):
            if "/schedule?" in url:
                waiting.set()
                await release.wait()
            return await self.source(url)
        with patch.object(league_data, "fetch_json", side_effect=source) as fetch:
            task = asyncio.create_task(team_center.maintain_team(self.store, "arsenal"))
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                await asyncio.sleep(0)
                view = team_center.center_view(self.store, "arsenal")
                self.assertEqual(len(view["roster"]), 2)
                self.assertEqual(view["components"]["roster"]["status"], "fresh")
                self.assertEqual(view["components"]["schedule"]["status"], "running")
            finally:
                release.set()
                await task
        self.assertEqual(fetch.await_count, 4)

    async def test_only_failed_component_is_retried_and_successful_timestamps_survive(self):
        async def source(url):
            if "/standings?" in url:
                raise IngestionError("积分来源暂不可用")
            return await self.source(url)
        with patch.object(league_data, "fetch_json", side_effect=source):
            await team_center.maintain_team(self.store, "arsenal")
        before = team_center.center_view(self.store, "arsenal")
        for key in ("team_maintenance_PL-2026_arsenal", "league_standings_PL-2026"):
            status = self.store.get_meta(key, {})
            status["last_attempt"] = OLD
            for value in status.get("components", {}).values():
                value["last_attempt"] = OLD
            self.store.set_meta(key, status)
        with patch.object(league_data, "fetch_json", side_effect=self.source) as fetch:
            await team_center.maintain_team(self.store, "arsenal")
        self.assertEqual(fetch.await_count, 1)
        self.assertIn("/standings?", fetch.await_args.args[0])
        after = team_center.center_view(self.store, "arsenal")
        self.assertEqual(after["roster_updated_at"], before["roster_updated_at"])
        self.assertEqual(after["team_schedule_updated_at"], before["team_schedule_updated_at"])

    async def test_partial_schedule_retries_only_failed_endpoint(self):
        async def first(url):
            if "fixture=true" in url:
                raise IngestionError("未来赛程暂不可用")
            return schedule_payload()
        with patch.object(league_data, "fetch_json", side_effect=first):
            await league_data.sync_team_recent(self.store, "arsenal")
        key = league_data.team_schedule_key(SEASON["id"], "arsenal")
        old = self.store.get_meta(key)
        self.store.set_meta(key, {**old, "last_attempt": OLD})
        with patch.object(league_data, "fetch_json", return_value=schedule_payload(future=True)) as fetch:
            result = await league_data.sync_team_recent(self.store, "arsenal")
        self.assertEqual(fetch.await_count, 1)
        self.assertIn("fixture=true", fetch.await_args.args[0])
        self.assertEqual(result["results_updated_at"], old["results_updated_at"])
        self.assertEqual(len(team_center.center_view(self.store, "arsenal")["matches"]), 2)

    async def test_partial_schedule_failure_remains_retryable_through_maintenance(self):
        async def first(url):
            if "fixture=true" in url:
                raise IngestionError("未来赛程暂不可用")
            return await self.source(url)
        with patch.object(league_data, "fetch_json", side_effect=first):
            await team_center.maintain_team(self.store, "arsenal")
        self.assertTrue(team_center.center_view(self.store, "arsenal")["components"]["schedule"]["stale"])
        for key in ("team_maintenance_PL-2026_arsenal", "team_schedule_PL-2026_arsenal"):
            status = self.store.get_meta(key)
            status["last_attempt"] = OLD
            for component in status.get("components", {}).values():
                component["last_attempt"] = OLD
            self.store.set_meta(key, status)
        with patch.object(league_data, "fetch_json", side_effect=self.source) as fetch:
            await team_center.maintain_team(self.store, "arsenal", force=True)
        self.assertEqual(fetch.await_count, 1)
        self.assertIn("fixture=true", fetch.await_args.args[0])

    async def test_two_clubs_share_one_standings_fetch(self):
        with patch.object(league_data, "fetch_json", return_value=standings()) as fetch:
            await asyncio.gather(league_data.sync_standings(self.store, "PL"), league_data.sync_standings(self.store, "PL"))
        self.assertEqual(fetch.await_count, 1)

    async def test_one_round_is_not_a_full_season_even_if_both_requests_succeed(self):
        with patch.object(league_data, "fetch_json", return_value=schedule_payload()):
            await league_data.sync_team_recent(self.store, "arsenal")
        view = team_center.center_view(self.store, "arsenal")
        self.assertEqual(view["coverage"]["matches"], 1)
        self.assertEqual(view["coverage"]["expected_matches"], 2)
        self.assertFalse(view["coverage"]["has_full_schedule"])
        self.assertEqual(view["components"]["schedule"]["status"], "partial")

    async def test_empty_roster_refresh_keeps_valid_snapshot_and_reports_failure(self):
        original = team_center.parse_roster(roster_payload(), TEAM, LEAGUE, SEASON, "359")
        self.store.put("team_snapshots", original)
        payload = roster_payload()
        payload["athletes"] = []
        with patch.object(league_data, "fetch_json", return_value=payload):
            await team_center.maintain_team(self.store, "arsenal", parts=["roster"])
        view = team_center.center_view(self.store, "arsenal")
        self.assertEqual(self.store.get("team_snapshots", original["id"]), original)
        self.assertEqual(view["components"]["roster"]["status"], "failed")
        self.assertTrue(view["components"]["roster"]["available"])
        self.assertEqual(view["components"]["roster"]["updated_at"], original["fetched_at"])

    async def test_manager_closes_optional_photo_task_after_core_finishes(self):
        waiting, release = asyncio.Event(), asyncio.Event()
        async def slow_photos(store, snapshot, *, force=False):
            self.assertFalse(force)
            waiting.set()
            await release.wait()
            return snapshot
        with patch.object(league_data, "fetch_json", side_effect=self.source), \
             patch.dict(team_portraits.DIRECTORIES, {"arsenal": {"url": "https://example.test/", "credit": "test"}}), \
             patch.object(team_portraits, "enrich_portraits", side_effect=slow_photos):
            manager = team_center.TeamMaintenanceManager(self.store)
            manager.start("arsenal")
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                self.assertTrue(all(task.done() for task in manager.tasks.values()))
                self.assertEqual(team_center.center_view(self.store, "arsenal")["components"]["portraits"]["status"], "running")
                await manager.close()
                self.assertTrue(all(task.done() for task in manager.portrait_tasks.values()))
            finally:
                release.set()
                await manager.close()
        self.assertNotEqual(team_center.center_view(self.store, "arsenal")["components"]["portraits"]["status"], "running")

    async def test_fresh_core_does_not_refetch_when_only_portraits_are_missing(self):
        with patch.object(league_data, "fetch_json", side_effect=self.source):
            await team_center.maintain_team(self.store, "arsenal")
        with patch.object(league_data, "fetch_json", side_effect=AssertionError("fresh core must be reused")) as fetch:
            result = await team_center.maintain_team(self.store, "arsenal")
        self.assertTrue(result["cached"])
        fetch.assert_not_called()

    async def test_old_cancelled_core_does_not_cancel_new_photo_only_wait(self):
        manager = team_center.TeamMaintenanceManager(self.store)
        key = "team_maintenance_PL-2026_arsenal"
        old = asyncio.create_task(asyncio.sleep(10))
        old.cancel()
        await asyncio.gather(old, return_exceptions=True)
        manager.tasks[key] = old
        started, release = asyncio.Event(), asyncio.Event()
        async def photo():
            started.set()
            await release.wait()
        manager.portrait_tasks[key] = asyncio.create_task(photo())
        await started.wait()
        release.set()
        try:
            await manager.wait_for_team("arsenal")
            self.assertTrue(manager.portrait_tasks[key].done())
            with patch.object(team_center, "sync_roster", new=AsyncMock(return_value={"cached": False, "updated_at": now()})):
                self.assertTrue(manager.start("chelsea", parts=["roster"])["started"])
                self.assertEqual((await manager.wait_for_team("chelsea"))["status"], "completed")
        finally:
            await manager.close()

    async def test_manager_finishes_combined_decision_after_photos_without_waiter(self):
        from server import decisions
        manager = team_center.TeamMaintenanceManager(self.store)
        decision_id = decisions.begin(self.store, "arsenal", team_center.center_view(self.store, "arsenal", reveal=True),
            parts=["roster", "portraits"], trigger="automatic", reason="更新阵容与照片。")
        async def photos(store, team_id, year=None):
            _, _, season, _ = team_center.team_season_context(store, team_id, year)
            snapshot_id = team_center._snapshot_id(team_id, season["id"])
            snapshot = store.get("team_snapshots", snapshot_id)
            snapshot["roster"][0]["photo_url"] = "https://upload.wikimedia.org/new.jpg"
            snapshot["roster"][0]["photo_source_url"] = "https://commons.wikimedia.org/wiki/File:New.jpg"
            snapshot["roster"][0]["photo_updated_at"] = now()
            store.put("team_snapshots", snapshot)
            team_center._record_component(store, "team_maintenance_PL-2026_arsenal", "portraits", {"status": "completed", "cached": False})
            return {"status": "completed", "cached": False}
        try:
            with patch.object(league_data, "fetch_json", side_effect=self.source), \
                 patch.object(team_center, "sync_portraits", side_effect=photos):
                manager.start("arsenal", parts=["roster", "portraits"], decision_id=decision_id)
                key = "team_maintenance_PL-2026_arsenal"
                await manager.tasks[key]
                if key in manager.portrait_tasks:
                    await manager.portrait_tasks[key]
            row = self.store.get("decisions", decision_id)
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["after"]["portraits"]["count"], 1)
            self.assertTrue(row["changes"][1]["changed"])
        finally:
            await manager.close()

    async def test_photo_only_decision_finishes_without_waiter(self):
        from server import decisions
        snapshot = team_center.parse_roster(roster_payload(), TEAM, LEAGUE, SEASON, "359")
        self.store.put("team_snapshots", snapshot)
        manager = team_center.TeamMaintenanceManager(self.store)
        decision_id = decisions.begin(self.store, "arsenal", team_center.center_view(self.store, "arsenal", reveal=True),
            parts=["portraits"], trigger="automatic", reason="补充照片。")
        async def photos(store, team_id, year=None):
            row = store.get("team_snapshots", snapshot["id"])
            row["roster"][0].update(photo_url="https://upload.wikimedia.org/new.jpg", photo_updated_at=now())
            store.put("team_snapshots", row)
            team_center._record_component(store, "team_maintenance_PL-2026_arsenal", "portraits", {"status": "completed", "cached": False})
            return {"status": "completed"}
        try:
            with patch.object(team_center, "sync_portraits", side_effect=photos):
                result = manager.start("arsenal", parts=["portraits"], decision_id=decision_id)
                self.assertTrue(result["started"])
                await manager.finalizers["team_maintenance_PL-2026_arsenal"]
            row = self.store.get("decisions", decision_id)
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["after"]["portraits"]["count"], 1)
        finally:
            await manager.close()

    async def test_cached_part_and_fetched_part_have_distinct_confirmation(self):
        from server import decisions
        snapshot = team_center.parse_roster(roster_payload(), TEAM, LEAGUE, SEASON, "359")
        self.store.put("team_snapshots", snapshot)
        async def refresh_standings(store, league_id, year, *, force=False):
            store.put("standings", {"id": "PL-2026", "rows": [{"team_id": "arsenal", "rank": 1}],
                "fetched_at": now(), "source_url": "https://www.espn.com/standings"})
            return {"sync_status": "completed", "cached": False}
        with patch.object(team_center, "sync_roster", new=AsyncMock(return_value={"cached": True, "updated_at": snapshot["fetched_at"]})), \
             patch.object(league_data, "sync_standings", side_effect=refresh_standings):
            await team_center.maintain_team(self.store, "arsenal", parts=["roster", "standings"])
        row = decisions.history(self.store, "arsenal")["items"][0]
        changes = {item["field"]: item for item in row["changes"]}
        self.assertFalse(changes["roster"]["confirmed"])
        self.assertTrue(changes["standings"]["confirmed"])

    async def test_parallel_photo_callers_share_one_source_pass(self):
        snapshot = team_center.parse_roster(roster_payload(), TEAM, LEAGUE, SEASON, "359")
        self.store.put("team_snapshots", snapshot)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def photos(store, snapshot, **kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return snapshot, {"status": "completed", "cached": False}
        with patch.object(team_portraits, "enrich_player_photos", side_effect=photos):
            first = asyncio.create_task(team_center.fetch_player_photos(self.store, "arsenal"))
            await entered.wait()
            second = asyncio.create_task(team_center.fetch_player_photos(self.store, "arsenal"))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual(calls, 1)

    async def test_cancelled_photo_waiter_does_not_stop_owner(self):
        snapshot = team_center.parse_roster(roster_payload(), TEAM, LEAGUE, SEASON, "359")
        self.store.put("team_snapshots", snapshot)
        entered, release = asyncio.Event(), asyncio.Event()
        async def photos(store, snapshot, **kwargs):
            entered.set()
            await release.wait()
            return snapshot, {"status": "completed", "cached": False}
        with patch.object(team_portraits, "enrich_player_photos", side_effect=photos):
            owner = asyncio.create_task(team_center.fetch_player_photos(self.store, "arsenal"))
            await entered.wait()
            waiter = asyncio.create_task(team_center.fetch_player_photos(self.store, "arsenal"))
            await asyncio.sleep(0)
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            self.assertFalse(owner.done())
            release.set()
            self.assertEqual((await owner)["status"], "completed")

    async def test_scheduler_close_propagates_through_photo_finalizer(self):
        snapshot = team_center.parse_roster(roster_payload(), TEAM, LEAGUE, SEASON, "359")
        self.store.put("team_snapshots", snapshot)
        self.store.save_settings({"followed_teams": ["arsenal"]})
        maintenance = team_center.TeamMaintenanceManager(self.store)
        scheduler = CollectionScheduler(self.store, maintenance)
        entered = asyncio.Event()
        async def photos(store, team_id, year=None):
            entered.set()
            await asyncio.Event().wait()
        need = collection.Need("portraits", "photo_url", 2.0, "照片待核对。", capability="image")
        try:
            with patch.object(collection, "needs_from", return_value=[need]), \
                 patch.object(team_center, "sync_portraits", side_effect=photos):
                self.assertTrue(scheduler.start(delay=0))
                await asyncio.wait_for(entered.wait(), 1)
                await asyncio.wait_for(scheduler.close(), 1)
            self.assertFalse(scheduler.status()["running"])
            self.assertTrue(scheduler.task.cancelled())
            row = decisions.history(self.store, "arsenal")["items"][0]
            self.assertEqual(row["status"], "interrupted")
            self.assertEqual(row["after"]["portraits"]["count"], 0)
        finally:
            if scheduler.task and not scheduler.task.done():
                scheduler.task.cancel()
                await asyncio.gather(scheduler.task, return_exceptions=True)
            await maintenance.close()


class SourceRetryTests(unittest.IsolatedAsyncioTestCase):
    async def fetch_responses(self, statuses):
        seen = []
        def respond(request):
            seen.append(request.url)
            return httpx.Response(statuses[min(len(seen) - 1, len(statuses) - 1)], json={"checked": True})
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(league_data.httpx, "AsyncClient", return_value=client):
            try:
                result = await league_data.fetch_json(league_data.API + "/apis/test")
            except IngestionError as exc:
                result = exc
        return result, seen

    async def test_transient_server_failure_has_one_retry(self):
        result, calls = await self.fetch_responses([503, 200])
        self.assertEqual(result, {"checked": True})
        self.assertEqual(len(calls), 2)

    async def test_persistent_failure_stops_after_two_and_forbidden_is_not_retried(self):
        for statuses, expected in (([503], 2), ([403], 1)):
            result, calls = await self.fetch_responses(statuses)
            self.assertIsInstance(result, IngestionError)
            self.assertEqual(len(calls), expected)


async def benchmark_refresh():
    """Offline comparison with the saved V7 baseline; no provider/model calls."""
    import json
    import subprocess
    import time
    import types
    baseline = types.ModuleType("server._team_center_baseline")
    baseline.__package__ = "server"
    source_code = subprocess.check_output(["git", "show", "v7.0.0:server/team_center.py"], text=True, encoding="utf-8")
    exec(compile(source_code, "v7.0.0:server/team_center.py", "exec"), baseline.__dict__)
    measurements = []
    for label, module in (("v7.0.0", baseline), ("current", team_center)):
        scenario = IndependentRefreshTests()
        scenario.setUp()
        calls = []
        async def source(url):
            calls.append(url)
            await asyncio.sleep(.03)
            if "/scoreboard?" in url:
                raise IngestionError("Simulated unavailable whole-league scoreboard")
            return await scenario.source(url)
        async def photos(store, snapshot):
            await asyncio.sleep(.2)
            return snapshot
        try:
            with patch.object(league_data, "fetch_json", side_effect=source), patch.object(team_portraits, "enrich_portraits", side_effect=photos):
                started = time.perf_counter()
                await module.maintain_team(scenario.store, "arsenal")
                elapsed = time.perf_counter() - started
            measurements.append({"version": label, "core_seconds": round(elapsed, 3), "requests": len(calls)})
        finally:
            scenario.tearDown()
    print(json.dumps({"offline_only": True, "source_delay_seconds": .03, "optional_photo_delay_seconds": .2,
                      "scenario": "whole-league scoreboard unavailable; team endpoints healthy", "measurements": measurements}, ensure_ascii=False))


if __name__ == "__main__":
    import sys
    if "--benchmark" in sys.argv:
        asyncio.run(benchmark_refresh())
    else:
        unittest.main()
