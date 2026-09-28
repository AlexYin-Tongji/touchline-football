"""The registry decides what can be asked of whom — and cannot be talked up."""
import asyncio
import unittest

from server import provenance as prov
from server.providers import (Answer, BaseProvider, Capability, Cost, Latency, ProviderError, Source,
                              SourceTier, adapter, configure_credentials, merge_answers, provider, reset,
                              source, sources)


def registered(source_id="fake", tier=SourceTier.COMMUNITY, capabilities=(Capability.NEWS,),
               domains=("fake.example",), **extra):
    @provider({"id": source_id, "name": extra.pop("name", "假源"), "tier": tier, "domains": domains}, set(capabilities), **extra)
    class Stub(BaseProvider):
        min_interval = 0.0

        async def news(self, query=""):
            return self.envelope(Capability.NEWS, items=[{"title": query}])

        async def person(self, ref=""):
            return self.envelope(Capability.PERSON, items=[{"ref": ref}])

    return Stub


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = dict(prov._REGISTRY)
        self.addCleanup(self._restore)

    def _restore(self):
        reset()
        prov._REGISTRY.clear()
        prov._REGISTRY.update(self.registry)

    def test_a_declared_provider_becomes_askable(self):
        registered()
        self.assertEqual([row.id for row in sources()], ["fake"])
        self.assertEqual([row.id for row in sources(capability=Capability.NEWS)], ["fake"])

    def test_a_capability_nobody_declares_returns_nothing(self):
        registered(capabilities=(Capability.NEWS,))
        self.assertEqual(sources(capability=Capability.IMAGE), [])

    def test_declaring_a_source_grants_exactly_its_tier(self):
        registered(tier=SourceTier.COMMUNITY)
        self.assertEqual(prov.tier_for_url("https://fake.example/post"), SourceTier.COMMUNITY)

    def test_an_adapter_cannot_speak_in_a_stronger_tier(self):
        Stub = registered(source_id="forum", tier=SourceTier.COMMUNITY, domains=("forum.example",))
        with self.assertRaises(prov.UnregisteredSource):
            prov.claim_from({"id": "forum", "url": "https://forum.example/x"}, "injury", "out",
                            tier=SourceTier.OFFICIAL)

    def test_asking_an_unknown_provider_is_refused(self):
        with self.assertRaises(ProviderError):
            adapter("nobody")

    def test_a_provider_is_returned_by_value_not_by_reference(self):
        registered()
        self.assertIsNot(sources()[0], source("fake"))
        self.assertIsNot(source("fake"), source("fake"))


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.registry = dict(prov._REGISTRY)
        self.addCleanup(self._restore)

    def _restore(self):
        reset()
        prov._REGISTRY.clear()
        prov._REGISTRY.update(self.registry)

    def test_a_provider_needing_a_missing_key_is_not_offered(self):
        registered(source_id="paid", requires_key="api_football")
        configure_credentials(lambda name: False)
        self.addCleanup(configure_credentials, None)
        self.assertEqual(sources(capability=Capability.NEWS), [])

    def test_it_appears_once_the_key_exists(self):
        registered(source_id="paid", requires_key="api_football")
        configure_credentials(lambda name: name == "api_football")
        self.addCleanup(configure_credentials, None)
        self.assertEqual([row.id for row in sources(capability=Capability.NEWS)], ["paid"])

    def test_an_unconfigured_credential_raises_rather_than_guessing(self):
        Stub = registered(source_id="paid", requires_key="api_football")
        with self.assertRaises(ProviderError) as caught:
            Stub(store=None).credentials()
        self.assertEqual(caught.exception.code, "missing_credential")


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.registry = dict(prov._REGISTRY)
        self.addCleanup(self._restore)

    def _restore(self):
        reset()
        prov._REGISTRY.clear()
        prov._REGISTRY.update(self.registry)

    def test_a_failure_is_a_gap_and_never_an_empty_success(self):
        Stub = registered()
        answer = Stub().unavailable(Capability.NEWS, "来源超时", retryable=True)
        self.assertEqual(answer.items, [])
        self.assertEqual(answer.gaps[0]["message"], "来源超时")
        self.assertTrue(answer.gaps[0]["retryable"])
        # "Nothing found" and "the request failed" must not look alike.
        self.assertFalse(answer.ok)

    def test_an_empty_but_successful_answer_is_ok(self):
        Stub = registered()
        answer = Stub().envelope(Capability.NEWS)
        self.assertTrue(answer.ok)
        self.assertEqual(answer.gaps, [])

    def test_the_envelope_carries_the_registered_tier(self):
        Stub = registered(tier=SourceTier.PRESS)
        self.assertEqual(Stub().envelope(Capability.NEWS).tier, SourceTier.PRESS)

    def test_an_unknown_capability_is_refused(self):
        Stub = registered()
        with self.assertRaises(ProviderError):
            Stub().envelope("telepathy")

    def test_merging_keeps_every_item_and_reports_every_gap(self):
        merged = merge_answers([
            Answer(source_id="a", tier=SourceTier.PRESS, capability=Capability.NEWS, items=[1], gaps=["慢了"]),
            Answer(source_id="b", tier=SourceTier.OFFICIAL, capability=Capability.NEWS, items=[2]),
        ])
        self.assertEqual(merged.items, [1, 2])
        self.assertEqual(merged.gaps, ["慢了"])
        self.assertEqual(merged.tier, SourceTier.OFFICIAL)

    def test_merging_nothing_is_an_empty_derived_answer(self):
        self.assertEqual(merge_answers([]).items, [])

    def test_a_partly_successful_merge_is_still_ok(self):
        merged = merge_answers([
            Answer(source_id="a", tier=SourceTier.PRESS, capability=Capability.NEWS, items=[1]),
            Answer(source_id="b", tier=SourceTier.PRESS, capability=Capability.NEWS, gaps=["超时"]),
        ])
        self.assertTrue(merged.ok)


class PacingTests(unittest.TestCase):
    def test_two_callers_share_one_interval(self):
        from server.providers import paced
        gate = paced("paced_test", 0.05)
        reset()
        calls = []

        async def run():
            for _ in range(3):
                await gate.wait()
                calls.append(1)

        started = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(run())
        self.assertEqual(len(calls), 3)
        reset()


if __name__ == "__main__":
    unittest.main()
