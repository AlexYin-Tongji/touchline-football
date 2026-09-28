"""The model can request a registered structured source only in its team scope."""
import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server.agent_tools import LocalTools, ToolError
from server import provenance
from server.providers import api_football
from server.providers import Answer, Capability, Cost, SourceTier
from server.storage import Store
from tests.test_agent import FakeStore


class StructuredSourceToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "isolated.sqlite3")
        fake = FakeStore()
        self.store.put("teams", fake.get("teams", "arsenal"))
        self.run = fake.new_run()
        self.store.put("runs", self.run)

    async def asyncTearDown(self):
        self.store.close()
        self.directory.cleanup()

    def local(self):
        return LocalTools(self.store, self.run, self.store.settings())

    async def test_discovery_gap_prevents_provider_query(self):
        self.store.set_secret("api_football_api_key", "test-key")
        discovery = Answer("api_football", SourceTier.DATASET, Capability.SQUAD,
                           gaps=[{"code": "team_not_found", "message": "未找到可核对的球队"}], spent=Cost.QUOTA)
        with patch.object(api_football, "resolve_team_scope", new_callable=AsyncMock,
                          return_value=discovery, create=True) as resolve, \
                patch("server.providers.query", new_callable=AsyncMock) as query:
            result = await self.local().acall("query_structured_sources", {"capability": "squad"})
        self.assertEqual(result["gaps"][0]["code"], "team_not_found")
        self.assertEqual(result["scope_resolution"]["spent"], "quota")
        resolve.assert_awaited_once_with(self.store, "arsenal", year=None)
        query.assert_not_awaited()
        with self.assertRaises(ToolError):
            await self.local().acall("query_structured_sources", {"capability": "squad", "team_id": 42})

    async def test_spoiler_and_match_scope_block_structured_lookup(self):
        with patch("server.providers.query", new_callable=AsyncMock) as query:
            result = await self.local().acall("query_structured_sources", {"capability": "fixture"})
            self.assertEqual(result["gaps"][0]["code"], "spoiler_scope")
            match = self.store.get("matches", "match_one")
            if match is None:
                match = FakeStore().get("matches", "match_one")
                self.store.put("matches", match)
            scoped = dict(self.run, skill="match_preview", match_id="match_one")
            local = LocalTools(self.store, scoped, self.store.settings())
            self.assertNotIn("query_structured_sources", {t["function"]["name"] for t in local.available_tools()})
            self.assertEqual((await local.acall("query_structured_sources", {"capability": "squad"}))["gaps"][0]["code"], "fixed_scope")
        query.assert_not_awaited()

    async def test_missing_key_is_an_explicit_gap(self):
        with patch.object(api_football, "resolve_team_scope", new_callable=AsyncMock, create=True) as resolve:
            result = await self.local().acall("query_structured_sources", {"capability": "squad"})
        self.assertEqual(result["gaps"][0]["code"], "missing_credential")
        self.assertFalse(result["items"])
        resolve.assert_not_awaited()

    async def test_resolved_team_uses_registry_and_no_model_supplied_id(self):
        self.store.set_secret("api_football_api_key", "test-key")
        discovery = Answer("api_football", SourceTier.DATASET, Capability.SQUAD,
                           items=[{"scope": {"team_id": 42, "league_id": 39, "season": 2026}}],
                           cached=False, spent=Cost.QUOTA)
        claim = provenance.claim_from(
            {"id": "api_football", "url": "https://v3.football.api-sports.io/"},
            "name", "Example", fetched_at="2026-09-28T08:00:00Z")
        answer = Answer("api_football", SourceTier.DATASET, Capability.SQUAD,
                        items=[{"provider_team": 42, "claims": {"name": claim}}], spent=Cost.FREE)
        with patch.object(api_football, "resolve_team_scope", new_callable=AsyncMock,
                          return_value=discovery, create=True) as resolve, \
                patch("server.providers.query", new_callable=AsyncMock, return_value=answer) as query:
            local = self.local()
            result = await local.acall("query_structured_sources", {"capability": "squad"})
        resolve.assert_awaited_once_with(self.store, "arsenal", year=None)
        query.assert_awaited_once_with("api_football", "squad", store=self.store, team_id=42)
        self.assertEqual(result["items"][0]["provider_team"], 42)
        self.assertEqual(result["items"][0]["claims"]["name"]["value"], "Example")
        self.assertEqual(result["scope_resolution"]["spent"], "quota")
        json.dumps(result)
        self.assertFalse(local.read_ids)

    async def test_fixtures_filter_team_before_truncation(self):
        self.store.set_secret("api_football_api_key", "test-key")
        self.store.save_settings({**self.store.settings(), "spoiler_free": False})
        discovery = Answer("api_football", SourceTier.DATASET, Capability.SQUAD,
                           items=[{"scope": {"team_id": 42, "league_id": 39, "season": 2026}}],
                           cached=True, spent=Cost.FREE)
        fixtures = [{"provider_fixture": index, "home_provider_team": 90, "away_provider_team": 91}
                    for index in range(60)]
        fixtures[50]["away_provider_team"] = 42
        answer = Answer("api_football", SourceTier.DATASET, Capability.FIXTURE,
                        items=fixtures, spent=Cost.QUOTA)
        with patch.object(api_football, "resolve_team_scope", new_callable=AsyncMock,
                          return_value=discovery, create=True), \
                patch("server.providers.query", new_callable=AsyncMock, return_value=answer) as query:
            result = await self.local().acall("query_structured_sources", {"capability": "fixture"})
        query.assert_awaited_once_with("api_football", "fixture", store=self.store,
                                       team_id=42, league_id=39, season=2026)
        self.assertEqual([row["provider_fixture"] for row in result["items"]], [50])
        self.assertEqual(result["total_count"], 1)

    async def test_unrelated_provider_rows_become_scope_gap(self):
        self.store.set_secret("api_football_api_key", "test-key")
        discovery = Answer("api_football", SourceTier.DATASET, Capability.SQUAD,
                           items=[{"scope": {"team_id": 42, "league_id": 39, "season": 2026}}])
        answer = Answer("api_football", SourceTier.DATASET, Capability.SQUAD,
                        items=[{"provider_team": 99, "claims": {"name": "Other Club"}}])
        with patch.object(api_football, "resolve_team_scope", new_callable=AsyncMock,
                          return_value=discovery), \
                patch("server.providers.query", new_callable=AsyncMock, return_value=answer):
            result = await self.local().acall("query_structured_sources", {"capability": "squad"})
        self.assertEqual(result["items"], [])
        self.assertEqual(result["gaps"][0]["code"], "team_scope_mismatch")
