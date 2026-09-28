"""A keyed provider costs quota, so it must never spend a call it can avoid."""
import asyncio
import json
import unittest
from pathlib import Path
import tempfile

from server import providers, provenance
from server.providers import Capability, SourceTier, configure_credentials, sources
from server.providers.football_data import FootballDataProvider
from server.storage import Store


def standings_payload():
    return {"standings": [{"table": [
        {"position": 1, "team": {"id": 57, "name": "Arsenal"}, "playedGames": 10, "points": 24},
        {"position": 2, "team": {"id": 65, "name": "Manchester City"}, "playedGames": 10, "points": 22},
    ]}]}


def matches_payload():
    return {"matches": [
        {"id": 1001, "utcDate": "2026-10-03T14:00:00Z", "status": "SCHEDULED",
         "homeTeam": {"id": 57, "name": "Arsenal"}, "awayTeam": {"id": 61, "name": "Chelsea"},
         "score": {"fullTime": {"home": None, "away": None}}, "matchday": 8},
        {"id": 1002, "utcDate": "2026-09-27T16:30:00Z", "status": "FINISHED",
         "homeTeam": {"id": 57, "name": "Arsenal"}, "awayTeam": {"id": 65, "name": "Manchester City"},
         "score": {"fullTime": {"home": 2, "away": 1}}, "matchday": 7},
    ]}


class RegistrationTests(unittest.TestCase):
    def test_it_speaks_as_a_dataset_source(self):
        row = next(item for item in sources(reachable=False) if item.id == "football_data")
        self.assertEqual(row.tier, SourceTier.DATASET)

    def test_its_host_carries_the_granted_tier(self):
        self.assertEqual(provenance.tier_for_url("https://api.football-data.org/v4/competitions/PL/standings"),
                         SourceTier.DATASET)

    def test_it_is_declared_as_metered_and_keyed(self):
        row = next(item for item in sources(reachable=False) if item.id == "football_data")
        self.assertEqual(row.cost.value, "key")
        self.assertEqual(row.requires_key, "football_data_api_key")

    def test_it_is_not_offered_without_its_key(self):
        # A task must never be told it can refresh standings when the provider
        # holding that capability cannot actually be called.
        configure_credentials(lambda name: False)
        self.addCleanup(configure_credentials, None)
        self.assertNotIn("football_data", [row.id for row in sources(capability=Capability.STANDINGS)])

    def test_it_appears_once_its_key_is_stored(self):
        configure_credentials(lambda name: name == "football_data_api_key")
        self.addCleanup(configure_credentials, None)
        self.assertIn("football_data", [row.id for row in sources(capability=Capability.STANDINGS)])

    def test_it_only_claims_the_competitions_it_was_reviewed_for(self):
        provider_ = FootballDataProvider(store=None)
        self.assertEqual(provider_.competition("premier-league"), "PL")
        self.assertIsNone(provider_.competition("some-other-league"))


class FootballDataCase(unittest.TestCase):
    """Shared setup: an isolated store, a stored key, and pacing turned off.

    Pacing is disabled because the real interval exists to protect a metered
    provider from being throttled, and a test makes one or two requests. Paying
    six seconds per provider per test would add minutes to the suite for no
    extra coverage.
    """

    def setUp(self):
        from server.providers import unpaced
        unpaced()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "app.sqlite3")
        self.addCleanup(self.store.close)
        self.store.set_secret("football_data_api_key", "secret-example-key")
        self.calls = []

    def read(self, awaitable):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        return loop.run_until_complete(awaitable)


class CacheTests(FootballDataCase):
    """Quota is the binding constraint, so cache is checked before any request."""

    def install(self, payload):
        """Answer requests with a payload and count every call."""
        import server.ingestion as ingestion
        async def fake_fetch(url, **kwargs):
            self.calls.append((url, kwargs))
            return json.dumps(payload).encode(), url, "application/json"
        saved = ingestion.fetch_document
        ingestion.fetch_document = fake_fetch
        self.addCleanup(setattr, ingestion, "fetch_document", saved)
        return FootballDataProvider(store=self.store)

    def test_a_second_read_is_served_from_cache(self):
        provider_ = self.install(standings_payload())
        first = self.read(provider_.standings("premier-league"))
        self.assertTrue(first.ok)
        self.assertFalse(first.cached)
        second = self.read(provider_.standings("premier-league"))
        self.assertTrue(second.cached)
        self.assertEqual(len(self.calls), 1)

    def test_the_key_travels_in_a_header_not_the_url(self):
        provider_ = self.install(standings_payload())
        self.read(provider_.standings("premier-league"))
        url, options = self.calls[0]
        self.assertNotIn("secret-example-key", url)
        self.assertEqual(options["headers"]["X-Auth-Token"], "secret-example-key")

    def test_the_key_never_reaches_stored_state(self):
        provider_ = self.install(standings_payload())
        self.read(provider_.standings("premier-league"))
        stored = json.dumps(self.store.get_meta("football_data_v1:"
            + f"https://api.football-data.org/v4/competitions/PL/standings", {}), ensure_ascii=False)
        self.assertNotIn("secret-example-key", stored)

    def test_a_missing_key_is_reported_rather_than_called(self):
        self.store.set_secret("football_data_api_key", None)
        provider_ = FootballDataProvider(store=self.store)
        answer = self.read(provider_.standings("premier-league"))
        self.assertEqual(answer.items, [])
        self.assertTrue(answer.gaps)
        self.assertEqual(self.calls, [])
        self.provider = provider_


class ClaimTests(FootballDataCase):
    def install(self, payload):
        import server.ingestion as ingestion
        async def fake_fetch(url, **kwargs):
            return json.dumps(payload).encode(), url, "application/json"
        saved = ingestion.fetch_document
        ingestion.fetch_document = fake_fetch
        self.addCleanup(setattr, ingestion, "fetch_document", saved)
        self.provider = FootballDataProvider(store=self.store)

    def test_a_table_row_becomes_an_attributed_claim(self):
        self.install(standings_payload())
        answer = self.read(self.provider.standings("premier-league"))
        self.assertEqual(len(answer.items), 2)
        claim = answer.items[0]["claim"]
        self.assertEqual(claim.source_id, "football_data")
        self.assertEqual(claim.tier, SourceTier.DATASET)
        self.assertEqual(claim.value, 1)

    def test_each_fixture_field_is_claimed_separately(self):
        # A disagreement about kickoff must not also read as one about the score.
        self.install(matches_payload())
        answer = self.read(self.provider.fixtures("premier-league"))
        claims = answer.items[1]["claims"]
        self.assertEqual(set(claims), {"kickoff", "status", "score"})
        self.assertEqual(claims["score"].value, "2-1")

    def test_an_unfinished_match_has_no_score_claim(self):
        self.install(matches_payload())
        answer = self.read(self.provider.fixtures("premier-league"))
        self.assertNotIn("score", answer.items[0]["claims"])

    def test_an_unreviewed_competition_is_a_gap(self):
        self.provider = FootballDataProvider(store=self.store)
        answer = self.read(self.provider.fixtures("some-other-league"))
        self.assertEqual(answer.items, [])
        self.assertTrue(answer.gaps)


class CoexistenceTests(unittest.TestCase):
    """Two providers answering one question is the point, and neither wins."""

    def test_a_kickoff_from_each_source_is_kept_side_by_side(self):
        one = provenance.Claim(field="kickoff", value="2026-10-03T14:00:00Z", source_id="espn",
                               tier=SourceTier.DATASET, fetched_at="2026-09-28T00:00:00+00:00")
        two = provenance.Claim(field="kickoff", value="2026-10-03T14:05:00Z", source_id="football_data",
                               tier=SourceTier.DATASET, fetched_at="2026-09-28T00:00:00+00:00")
        merged = provenance.merge_claims([one, two])
        self.assertEqual(len(merged), 2)
        self.assertEqual(len(provenance.conflicts([one, two])), 1)

    def test_agreement_between_two_sources_is_recorded_as_such(self):
        one = provenance.Claim(field="kickoff", value="2026-10-03T14:00:00Z", source_id="espn",
                               tier=SourceTier.DATASET, fetched_at="2026-09-28T00:00:00+00:00")
        two = provenance.Claim(field="kickoff", value="2026-10-03T14:00:00Z", source_id="football_data",
                               tier=SourceTier.DATASET, fetched_at="2026-09-28T00:00:00+00:00")
        merged = provenance.merge_claims([one, two])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].derivation["merged_from"], ["espn", "football_data"])
        self.assertEqual(provenance.conflicts([one, two]), [])


if __name__ == "__main__":
    unittest.main()
