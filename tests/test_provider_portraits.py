"""The community database speaks as a community source, club field included."""
import tempfile
import unittest
from pathlib import Path

from server import provenance as prov
from server.providers import Capability, SourceTier, source, sources
from server.providers import portraits as portraits_module
from server.providers.portraits import TheSportsDbProvider, WikimediaProvider, portrait_providers
from server.storage import Store


class TierTests(unittest.TestCase):
    """A licence judgement must not be implied by the order two calls happen."""

    def test_wikimedia_is_a_dataset_source(self):
        self.assertEqual(source("wikimedia").tier, SourceTier.DATASET)

    def test_the_community_database_is_a_community_source(self):
        self.assertEqual(source("thesportsdb").tier, SourceTier.COMMUNITY)

    def test_their_hosts_carry_the_granted_tier(self):
        self.assertEqual(prov.tier_for_url("https://commons.wikimedia.org/wiki/File:X.jpg"), SourceTier.DATASET)
        self.assertEqual(prov.tier_for_url("https://r2.thesportsdb.com/images/media/player/cutout/a.png"),
                         SourceTier.COMMUNITY)

    def test_both_can_answer_a_portrait_question(self):
        self.assertEqual({row.id for row in portrait_providers()}, {"wikimedia", "thesportsdb"})

    def test_a_community_host_cannot_be_promoted_by_asking_nicely(self):
        with self.assertRaises(prov.UnregisteredSource):
            prov.claim_from({"id": "thesportsdb", "url": "https://www.thesportsdb.com/player/1"},
                            "photo_url", "https://x.example/a.png", tier=SourceTier.OFFICIAL)


class ClubRelationTests(unittest.TestCase):
    """The club field names a club without saying which relationship it means.

    A player contracted to one club and on loan at another is at both. Reading
    the second as a data error is what previously discarded a correct record.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "app.sqlite3")
        self.addCleanup(self.store.close)
        self.provider = TheSportsDbProvider(store=self.store)

    def test_the_club_value_is_returned_rather_than_discarded(self):
        result = self.provider.club({"strTeam": "Aston Villa", "strPlayer": "Someone"})
        self.assertIsNotNone(result)
        self.assertEqual(result["claim"].value, "Aston Villa")

    def test_the_relation_is_left_unstated(self):
        result = self.provider.club({"strTeam": "Aston Villa"})
        self.assertEqual(result["relation"], "unstated")
        self.assertIn("未声明", result["claim"].note)

    def test_the_club_claim_speaks_in_the_community_tier(self):
        result = self.provider.club({"strTeam": "Aston Villa"})
        self.assertEqual(result["claim"].tier, SourceTier.COMMUNITY)

    def test_a_missing_club_field_produces_nothing(self):
        self.assertIsNone(self.provider.club({}))
        self.assertIsNone(self.provider.club({"strTeam": ""}))

    def test_a_parent_club_and_a_loan_club_are_both_kept(self):
        # The structured roster says the parent, the community record says the
        # loan club. Neither is discarded and neither is reported as a conflict
        # once the relation is known.
        community = self.provider.club({"strTeam": "Aston Villa"})["claim"]
        parent = prov.Claim(field="club_id", value="Arsenal", source_id="espn", tier=SourceTier.DATASET,
                            fetched_at=prov.iso(None), derivation={"relation": "parent_club"})
        self.assertEqual(len(prov.merge_claims([community, parent])), 2)


class GapTests(unittest.TestCase):
    """A throttle must not read as "this player has no photo"."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "app.sqlite3")
        self.addCleanup(self.store.close)

    def test_a_missing_roster_nationality_is_reported_with_a_reason(self):
        provider = TheSportsDbProvider(store=self.store)
        import asyncio
        answer = asyncio.run(provider.portrait({"name": "Someone"}, "arsenal"))
        self.assertEqual(answer.items, [])
        self.assertTrue(answer.gaps)
        self.assertFalse(answer.ok)

    def test_retired_name_search_does_not_call_the_publisher(self):
        provider = TheSportsDbProvider(store=self.store)
        import asyncio
        called = False

        async def boom(*args, **kwargs):
            nonlocal called
            called = True
            raise RuntimeError("限流")
        import server.community_portraits as community
        saved = community.resolve_portrait
        community.resolve_portrait = boom
        self.addCleanup(setattr, community, "resolve_portrait", saved)
        answer = asyncio.run(provider.portrait({"name": "Someone", "nationality": "英格兰"}, "arsenal"))
        self.assertFalse(called)
        self.assertFalse(answer.ok)
        self.assertFalse(answer.gaps[0]["retryable"])
        self.assertIn("姓名搜索", answer.gaps[0]["message"])


class RegistrationShapeTests(unittest.TestCase):
    def test_both_providers_declare_the_same_capabilities(self):
        for name in ("wikimedia", "thesportsdb"):
            row = source(name)
            self.assertTrue({Capability.PERSON, Capability.IMAGE} <= row.capabilities)

    def test_the_community_provider_is_paced(self):
        self.assertGreater(TheSportsDbProvider.min_interval, 0)
        self.assertGreater(WikimediaProvider.min_interval, 0)

    def test_wikimedia_is_paced_more_slowly_than_the_community_source(self):
        # Wikimedia throttles a shared budget across three hosts.
        self.assertGreater(WikimediaProvider.min_interval, TheSportsDbProvider.min_interval)


if __name__ == "__main__":
    unittest.main()
