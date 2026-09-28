"""The keyed adapter must conserve a persistent daily request budget."""
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from server import providers
from server.provider_quota import DailyQuota
from server.providers import Capability, Cost, SourceTier
from server.storage import Store


def _payload(response, *, total_pages=1):
    return json.dumps({"get": "", "parameters": {}, "errors": [], "results": len(response),
                       "paging": {"current": 1, "total": total_pages},
                       "response": response}).encode()


def _fixture():
    return {"fixture": {"id": 10, "date": "2026-09-29T19:00:00+00:00",
                        "status": {"short": "NS"}},
            "league": {"id": 39, "season": 2026},
            "teams": {"home": {"id": 1, "name": "Home"},
                      "away": {"id": 2, "name": "Away"}},
            "goals": {"home": None, "away": None}}


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "quota.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.start = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)

    def test_reservations_survive_reopen_and_rolling_boundary(self):
        quota = DailyQuota(self.store, "api_football", limit=2)
        self.assertTrue(quota.reserve(now=self.start).allowed)
        self.assertTrue(quota.reserve(now=self.start + timedelta(hours=23)).allowed)
        second = Store(self.path)
        self.addCleanup(second.close)
        blocked = DailyQuota(second, "api_football", limit=2).reserve(now=self.start + timedelta(hours=23, minutes=1))
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.next_available_at, (self.start + timedelta(hours=24)).isoformat())
        self.assertTrue(DailyQuota(second, "api_football", limit=2).reserve(
            now=self.start + timedelta(hours=24, seconds=1)).allowed)
        self.assertEqual(quota.status(now=self.start + timedelta(hours=24, seconds=1))["used"], 2)

    def test_two_store_instances_cannot_overbook(self):
        from concurrent.futures import ThreadPoolExecutor
        other = Store(self.path)
        self.addCleanup(other.close)
        quotas = [DailyQuota(self.store, "api_football", limit=1),
                  DailyQuota(other, "api_football", limit=1)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            decisions = list(pool.map(lambda q: q.reserve(now=self.start), quotas))
        self.assertEqual(sum(row.allowed for row in decisions), 1)

    def test_remote_limit_pauses_even_when_local_budget_remains(self):
        quota = DailyQuota(self.store, "api_football")
        next_at = quota.pause_for_rate_limit(now=self.start)
        state = quota.reserve(now=self.start + timedelta(seconds=30))
        self.assertFalse(state.allowed)
        self.assertEqual(state.next_available_at, next_at)
        self.assertTrue(quota.reserve(now=self.start + timedelta(minutes=1)).allowed)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "app.sqlite3")
        self.addCleanup(self.store.close)
        self.store.set_secret("api_football_api_key", "test-key")
        providers.unpaced()
        self.calls = []

    def fetch(self, body):
        def fake(url, **options):
            self.calls.append((url, options))
            return body, url, "application/json"
        return patch("server.ingestion.fetch_document", fake)

    async def test_registration_and_capability_call_return_attributed_fixture(self):
        row = providers.source("api_football")
        self.assertEqual(row.tier, SourceTier.DATASET)
        self.assertEqual(row.requires_key, "api_football_api_key")
        self.assertIn(Capability.FIXTURE, row.capabilities)
        with self.fetch(_payload([_fixture()])):
            answer = await providers.query("api_football", Capability.FIXTURE, store=self.store,
                                           league_id=39, season=2026)
        self.assertEqual(answer.spent, Cost.QUOTA)
        self.assertEqual(answer.items[0]["claims"]["kickoff"].source_id, "api_football")
        self.assertEqual(answer.items[0]["home_provider_team"], 1)
        url, options = self.calls[0]
        self.assertNotIn("test-key", url)
        self.assertEqual(options["headers"]["x-apisports-key"], "test-key")
        self.assertEqual(DailyQuota(self.store, "api_football").status()["used"], 1)

    async def test_cache_avoids_second_reservation(self):
        with self.fetch(_payload([_fixture()])):
            first = await providers.query("api_football", "fixture", store=self.store,
                                          league_id=39, season=2026)
            second = await providers.query("api_football", "fixture", store=self.store,
                                           league_id=39, season=2026)
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(second.spent, Cost.FREE)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(DailyQuota(self.store, "api_football").status()["used"], 1)

    async def test_cached_claims_keep_original_fetch_time(self):
        from server.providers.api_football import ApiFootballProvider
        adapter = ApiFootballProvider(store=self.store)
        cases = [
            (adapter.fixtures, (39, 2026), _payload([_fixture()]),
             lambda answer: answer.items[0]["claims"]["kickoff"]),
            (adapter.standings, (39, 2026), _payload([{"league": {
                "id": 39, "season": 2026, "standings": [[{
                    "team": {"id": 1, "name": "Home"}, "rank": 1}]]}}]),
             lambda answer: answer.items[0]["claim"]),
            (adapter.squad, (1,), _payload([{"team": {"id": 1},
                "players": [{"id": 7, "name": "Player"}]}]),
             lambda answer: answer.items[0]["claims"]["name"]),
        ]
        for method, args, body, claim in cases:
            with self.fetch(body):
                first = await method(*args)
            with patch("server.providers.api_football._now", return_value="2030-01-01T00:00:00+00:00"):
                second = await method(*args)
            self.assertTrue(second.cached)
            self.assertEqual(claim(first).fetched_at, claim(second).fetched_at)

    async def test_multiple_pages_are_reported_as_incomplete(self):
        with self.fetch(_payload([_fixture()], total_pages=2)):
            answer = await providers.query("api_football", "fixture", store=self.store,
                                           league_id=39, season=2026)
        self.assertEqual(answer.gaps[0]["code"], "incomplete_page")
        self.assertEqual(answer.spent, Cost.QUOTA)
        self.assertEqual(answer.items, [])

    async def test_failure_spends_and_429_reports_next_time(self):
        def limited(url, **options):
            self.calls.append((url, options))
            from server.ingestion import IngestionError
            raise IngestionError("来源返回 HTTP 429")
        with patch("server.ingestion.fetch_document", limited):
            answer = await providers.query("api_football", "fixture", store=self.store,
                                           league_id=39, season=2026)
        self.assertEqual(answer.spent, Cost.QUOTA)
        self.assertEqual(answer.gaps[0]["code"], "http_rate_limited")
        self.assertTrue(answer.gaps[0]["next_available_at"])
        retry = datetime.fromisoformat(answer.gaps[0]["next_available_at"])
        self.assertLess(retry - datetime.now(timezone.utc), timedelta(minutes=2))
        self.assertEqual(DailyQuota(self.store, "api_football").status()["used"], 1)
        with self.fetch(_payload([_fixture()])):
            blocked = await providers.query("api_football", "fixture", store=self.store,
                                            league_id=39, season=2026)
        self.assertEqual(blocked.gaps[0]["code"], "quota_exhausted")
        self.assertEqual(len(self.calls), 1)

    async def test_explicit_daily_limit_pauses_for_24_hours(self):
        body = json.dumps({"errors": {"requests": "You have reached the request limit for the day"},
                           "response": [], "paging": {"current": 1, "total": 1}}).encode()
        with self.fetch(body):
            answer = await providers.query("api_football", "fixture", store=self.store,
                                           league_id=39, season=2026)
        retry = datetime.fromisoformat(answer.gaps[0]["next_available_at"])
        remaining = retry - datetime.now(timezone.utc)
        self.assertGreater(remaining, timedelta(hours=23))
        self.assertLessEqual(remaining, timedelta(hours=24))

    async def test_invalid_scope_and_missing_key_do_not_spend(self):
        from server.providers.api_football import ApiFootballProvider
        adapter = ApiFootballProvider(store=self.store)
        invalid = await adapter.fixtures("premier-league", 2026)
        self.assertEqual(invalid.gaps[0]["code"], "provider_scope_required")
        self.store.set_secret("api_football_api_key", None)
        missing = await adapter.squad(1)
        self.assertEqual(missing.gaps[0]["code"], "missing_credential")
        through_registry = await providers.query("api_football", "squad", store=self.store,
                                                 team_id=1)
        self.assertEqual(through_registry.gaps[0]["code"], "missing_credential")
        self.assertEqual(through_registry.spent, Cost.FREE)
        self.assertEqual(DailyQuota(self.store, "api_football").status()["used"], 0)

    async def test_squad_and_standings_reject_wrong_identity(self):
        from server.providers.api_football import ApiFootballProvider
        adapter = ApiFootballProvider(store=self.store)
        with self.fetch(_payload([{"team": {"id": 2}, "players": [{"id": 3, "name": "Player"}]}])):
            wrong = await adapter.squad(1)
        self.assertEqual(wrong.gaps[0]["code"], "identity_mismatch")
        with self.fetch(_payload([{"league": {"id": 39, "season": 2026, "standings": [[
                {"team": {"id": 1, "name": "Home"}, "rank": 1, "points": 3,
                 "all": {"played": 1}}]]}}])):
            table = await adapter.standings(39, 2026)
        self.assertEqual(table.items[0]["provider_team"], 1)
        self.assertEqual(table.items[0]["claim"].value, 1)

    async def test_same_url_concurrent_waiters_share_one_request_and_quota(self):
        import asyncio
        from server.providers.api_football import ApiFootballProvider
        def slow(url, **options):
            time.sleep(.05)
            self.calls.append((url, options))
            return _payload([_fixture()]), url, "application/json"
        with patch("server.ingestion.fetch_document", slow):
            answers = await asyncio.gather(*[
                ApiFootballProvider(store=self.store).fixtures(39, 2026) for _ in range(6)])
        self.assertTrue(all(answer.items for answer in answers))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(DailyQuota(self.store, "api_football").status()["used"], 1)

    async def test_cancel_one_waiter_keeps_other_waiter_alive(self):
        import asyncio
        from server.providers.api_football import ApiFootballProvider
        started, release = threading.Event(), threading.Event()
        def slow(url, **options):
            self.calls.append((url, options))
            started.set()
            release.wait(timeout=2)
            return _payload([_fixture()]), url, "application/json"
        with patch("server.ingestion.fetch_document", slow):
            first = asyncio.create_task(ApiFootballProvider(store=self.store).fixtures(39, 2026))
            second = asyncio.create_task(ApiFootballProvider(store=self.store).fixtures(39, 2026))
            await asyncio.to_thread(started.wait, 2)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            result = await asyncio.wait_for(second, 2)
        self.assertTrue(result.items)
        self.assertEqual(len(self.calls), 1)

    async def test_queued_different_url_observes_first_requests_429_before_reserving(self):
        import asyncio
        from server.ingestion import IngestionError
        from server.providers.api_football import ApiFootballProvider
        first_started, release_first = threading.Event(), threading.Event()
        second_paced, release_second = asyncio.Event(), asyncio.Event()
        pace_count = 0

        async def controlled_pace(enabled):
            nonlocal pace_count
            pace_count += 1
            if pace_count == 2:
                second_paced.set()
                await release_second.wait()

        def fetch(url, **options):
            self.calls.append((url, options))
            if len(self.calls) == 1:
                first_started.set()
                release_first.wait(timeout=2)
                raise IngestionError("来源返回 HTTP 429")
            return _payload([_fixture()]), url, "application/json"

        with patch("server.providers.api_football._pace", controlled_pace), \
                patch("server.ingestion.fetch_document", fetch):
            first = asyncio.create_task(ApiFootballProvider(store=self.store).fixtures(39, 2026))
            self.assertTrue(await asyncio.to_thread(first_started.wait, 2))
            second = asyncio.create_task(ApiFootballProvider(store=self.store).fixtures(39, 2025))
            await asyncio.wait_for(second_paced.wait(), 2)
            release_first.set()
            first_answer = await asyncio.wait_for(first, 2)
            self.assertEqual(first_answer.gaps[0]["code"], "http_rate_limited")
            release_second.set()
            second_answer = await asyncio.wait_for(second, 2)
        self.assertEqual(second_answer.gaps[0]["code"], "quota_exhausted")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(DailyQuota(self.store, "api_football").status()["used"], 1)

    async def test_standings_preserve_provider_update_time(self):
        from server.providers.api_football import ApiFootballProvider
        row = {"league": {"id": 39, "season": 2026, "standings": [[
            {"team": {"id": 1, "name": "Home"}, "rank": 1,
             "update": "2026-09-27T18:00:00+00:00"}]]}}
        adapter = ApiFootballProvider(store=self.store)
        with self.fetch(_payload([row])):
            first = await adapter.standings(39, 2026)
            second = await adapter.standings(39, 2026)
        claim = second.items[0]["claim"]
        self.assertEqual(claim.observed_at, "2026-09-27T18:00:00+00:00")
        self.assertEqual(claim.fetched_at, first.items[0]["claim"].fetched_at)


class ScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "app.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        from server.club_library import seed_clubs
        from server.league_catalog import seed_league_catalog
        seed_clubs(self.store)
        seed_league_catalog(self.store)
        self.store.set_secret("api_football_api_key", "test-key")
        providers.unpaced()
        self.calls = []

    def fake_fetch(self, url, **options):
        self.calls.append(url)
        if "/leagues?" in url:
            response = [{"league": {"id": 39, "name": "Premier League", "type": "League"},
                         "country": {"name": "England"}, "seasons": [{"year": 2026}]}]
        elif "/teams?" in url:
            response = [{"team": {"id": 42, "name": "Arsenal"}},
                        {"team": {"id": 43, "name": "Chelsea"}}]
        elif "/fixtures?" in url:
            fixture = _fixture()
            fixture["teams"]["home"]["id"] = 42
            response = [fixture]
        else:
            raise AssertionError(url)
        return _payload(response), url, "application/json"

    async def test_seeded_user_with_key_resolves_fixture_scope_and_reuses_it_after_restart(self):
        from server.providers.api_football import resolve_team_scope
        with patch("server.ingestion.fetch_document", self.fake_fetch):
            first = await resolve_team_scope(self.store, "arsenal")
            self.assertEqual(first.items[0]["scope"],
                             {"team_id": 42, "league_id": 39, "season": 2026})
            answer = await providers.query("api_football", "fixture", store=self.store,
                                           **first.items[0]["scope"])
        self.assertEqual(answer.items[0]["home_provider_team"], 42)
        self.assertEqual(len(self.calls), 3)
        reopened = Store(self.path)
        self.addCleanup(reopened.close)
        with patch("server.ingestion.fetch_document", side_effect=AssertionError("unexpected network")):
            second = await resolve_team_scope(reopened, "arsenal")
        self.assertTrue(second.cached)
        self.assertEqual(second.items[0]["scope"], first.items[0]["scope"])
        self.assertEqual(DailyQuota(reopened, "api_football").status()["used"], 3)

    async def test_missing_key_and_ambiguous_team_do_not_persist_scope(self):
        from server.providers.api_football import resolve_team_scope
        self.store.set_secret("api_football_api_key", None)
        missing = await resolve_team_scope(self.store, "arsenal")
        self.assertEqual(missing.gaps[0]["code"], "missing_credential")
        self.assertEqual(self.calls, [])
        self.store.set_secret("api_football_api_key", "test-key")
        def ambiguous(url, **options):
            if "/teams?" in url:
                return _payload([{"team": {"id": 42, "name": "Arsenal"}},
                                 {"team": {"id": 99, "name": "Arsenal"}}]), url, "application/json"
            return self.fake_fetch(url, **options)
        with patch("server.ingestion.fetch_document", ambiguous):
            result = await resolve_team_scope(self.store, "arsenal")
        self.assertEqual(result.gaps[0]["code"], "ambiguous_identity")
        self.assertEqual(self.store.get_meta("api_football_scope_v1:arsenal:2026"), None)
