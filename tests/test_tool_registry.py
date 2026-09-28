"""One call reads the context three tools used to read separately.

The stored runs show the shape of the problem: `context_pack`'s three
predecessors, plus the tools that were never called at all, were still offered
to every task. A tool the model cannot use in that scope is worse than absent —
it invites a call that fails, and the runs contain a hundred-percent failure
rate on exactly that pattern.
"""
import unittest

from server import tool_registry as registry
from server.agent_tools import LEGACY_TOOL_NAMES, TOOL_DEFINITIONS, tool_definitions
from tests.test_agent import FakeStore
from server.agent_tools import LocalTools


class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.names = [tool["name"] for tool in TOOL_DEFINITIONS]

    def test_the_context_pack_replaces_three_prompts(self):
        self.assertIn("context_pack", self.names)
        for retired in ("team_context", "source_overview", "reader_profile"):
            self.assertNotIn(retired, self.names)

    def test_every_retired_name_still_resolves(self):
        for retired, replacement in LEGACY_TOOL_NAMES.items():
            self.assertIn(replacement, self.names, retired)

    def test_a_retired_name_is_still_dispatchable(self):
        store = FakeStore()
        local = LocalTools(store, store.new_run(), store.settings())
        self.assertTrue(local.call("team_context", {}))
        self.assertTrue(local.call("source_overview", {}))
        self.assertTrue(local.call("reader_profile", {}))

    def test_the_pack_carries_what_the_three_used_to(self):
        store = FakeStore()
        local = LocalTools(store, store.new_run(), store.settings())
        pack = local.call("context_pack", {})
        for key in ("team", "events", "matches", "saved_knowledge", "sources", "reader", "notices"):
            self.assertIn(key, pack)

    def test_the_pack_still_says_its_indexes_are_not_evidence(self):
        store = FakeStore()
        local = LocalTools(store, store.new_run(), store.settings())
        pack = local.call("context_pack", {})
        self.assertTrue(any("不是已读证据" in note for note in pack["notices"]))

    def test_the_pack_does_not_read_a_source(self):
        store = FakeStore()
        local = LocalTools(store, store.new_run(), store.settings())
        local.call("context_pack", {})
        self.assertEqual(local.read_ids, set())


class CapabilitySelectionTests(unittest.TestCase):
    """A task declares work, and the registry returns the tools for it."""

    def test_a_news_task_is_not_offered_a_reader_preference_tool(self):
        chosen = registry.select([{"name": "reader_profile"}], registry.NEWS_WORK)
        self.assertEqual(chosen, [])

    def test_a_news_task_is_offered_the_pack_it_needs(self):
        chosen = registry.select([{"name": "context_pack"}, {"name": "read_evidence"}], registry.NEWS_WORK)
        self.assertEqual({tool["name"] for tool in chosen}, {"context_pack", "read_evidence"})

    def test_delegation_tools_travel_together(self):
        chosen = registry.select([{"name": name} for name in registry.DELEGATION_TOOLS],
                                 registry.NEWS_WORK)
        self.assertEqual(len(chosen), len(registry.DELEGATION_TOOLS))

    def test_a_task_without_delegation_gets_none_of_them(self):
        chosen = registry.select([{"name": "spawn_agent"}], frozenset({registry.EVIDENCE}))
        self.assertEqual(chosen, [])

    def test_an_unknown_workflow_is_an_error_not_an_empty_set(self):
        with self.assertRaises(registry.UnknownCapability):
            registry.declared("something_new")

    def test_every_shipped_workflow_has_a_capability_set(self):
        from server.skill_catalog import WORKFLOWS
        for name in WORKFLOWS:
            self.assertTrue(registry.declared(name), name)

    def test_the_allowlist_is_applied_after_the_capability(self):
        # A sub-task can never receive a tool its parent did not hold, even
        # when the declared work would cover it.
        chosen = registry.select([{"name": "context_pack"}], registry.NEWS_WORK, allowed={"read_evidence"})
        self.assertEqual(chosen, [])

    def test_policy_exclusion_still_wins(self):
        chosen = registry.select([{"name": "context_pack"}], registry.NEWS_WORK, excluded={"context_pack"})
        self.assertEqual(chosen, [])

    def test_no_tool_is_registered_without_a_capability(self):
        defined = {tool["name"] for tool in tool_definitions("ask")} | {"revise_news"}
        missing = defined - set(registry.TOOL_CAPABILITIES) - set(registry.DELEGATION_TOOLS)
        self.assertEqual(missing, set())


if __name__ == "__main__":
    unittest.main()
