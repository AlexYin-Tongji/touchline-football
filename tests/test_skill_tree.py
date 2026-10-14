"""What a skill's tree promises, and what a direction may and may not change.

The tree is data: a direction names a file, and that name is the only thing a
caller may ask for. These tests pin the properties that make that safe — a
direction cannot be reached across skills, cannot widen a task's scope, and
cannot put a background tool back once the run's own scope has forbidden it.
"""
import unittest

from server import agent, skill_catalog, tool_registry
from server.agent_tools import LocalTools, ToolError
from tests.test_agent import FakeStore


def names(local):
    return {tool["function"]["name"] for tool in local.available_tools()}


class SkillTreeTests(unittest.TestCase):
    def test_every_topic_names_a_file_that_exists(self):
        for skill, topics in skill_catalog.TOPICS.items():
            for topic in topics:
                path = skill_catalog.resolve_topic(skill, topic)
                self.assertTrue((skill_catalog.SKILL_ROOT / path).is_file(), path)

    def test_a_topic_is_only_reachable_within_its_own_skill(self):
        with self.assertRaises(ValueError):
            skill_catalog.resolve_topic("team-news", "squad.images")
        with self.assertRaises(ValueError):
            skill_catalog.resolve_topic("team-information", "nope")

    def test_a_task_starts_with_the_background_tools_withheld(self):
        # The default is unchanged: a conversation does not carry the slow
        # maintenance tools until a direction asks for one.
        store = FakeStore()
        local = LocalTools(store, store.new_run(skill="team_maintenance"),
                           store.settings(), agent.load_skill)
        self.assertNotIn("fetch_player_photos", names(local))

    def test_loading_the_image_direction_offers_the_photo_batch_tool(self):
        store = FakeStore()
        local = LocalTools(store, store.new_run(skill="team_maintenance"),
                           store.settings(), agent.load_skill)
        local.call("use_skill", {"skill": "team-information", "topic": "squad.images"})
        offered = names(local)
        self.assertIn("fetch_player_photos", offered)
        self.assertIn("photo_queue", offered)
        self.assertIn("plan_photo_batch", offered)

    def test_the_image_direction_drops_the_readers_it_does_not_need(self):
        # Narrowing is the point of loading a direction: the photo branch reads
        # a local queue and runs lookups, so the article and evidence readers are
        # not part of its work.
        store = FakeStore()
        local = LocalTools(store, store.new_run(skill="team_maintenance"),
                           store.settings(), agent.load_skill)
        before = names(local)
        local.call("use_skill", {"skill": "team-information", "topic": "squad.images"})
        after = names(local)
        self.assertTrue(before - after)
        for dropped in ("read_article", "discover_news", "research_news", "read_evidence"):
            self.assertNotIn(dropped, after)

    def test_scope_still_wins_over_a_direction_that_asks_for_a_tool(self):
        # Relevance may restore a background tool; permission may not be undone.
        for label, adjust, expect in (
                ("demo", lambda local: setattr(local, "mode", "demo"),
                 "get_team_data"),
                ("no network", lambda local: setattr(local, "allow_network", False),
                 "fetch_player_photos"),
                ("fixed article", lambda local: local.run.update(report_id="r1"),
                 "get_team_data"),
                ("fixed match", lambda local: setattr(local, "match", {"id": "m1"}), "get_team_data")):
            with self.subTest(label=label):
                store = FakeStore()
                local = LocalTools(store, store.new_run(skill="team_maintenance"),
                                   store.settings(), agent.load_skill)
                adjust(local)
                try:
                    local.call("use_skill", {"skill": "team-information", "topic": "squad.images"})
                except ToolError:
                    pass
                self.assertNotIn(expect, names(local))

    def test_a_direction_cannot_widen_the_capability_set(self):
        # Every narrowing is an intersection, so a direction declaring more than
        # its skill holds cannot hand the task anything new.
        wide = frozenset({tool_registry.NEWS, tool_registry.EVIDENCE})
        declared = tool_registry.topic_declared("team-information", "squad.images")["capabilities"]
        narrowed = frozenset(tool_registry.declared("team_maintenance")) & frozenset(declared)
        self.assertFalse(narrowed & wide)

    def test_a_repeat_load_returns_no_instructions_again(self):
        store = FakeStore()
        local = LocalTools(store, store.new_run(skill="team_maintenance"),
                           store.settings(), agent.load_skill)
        first = local.call("use_skill", {"skill": "team-information", "topic": "squad.images"})
        self.assertTrue(first["instructions"])
        again = local.call("use_skill", {"skill": "team-information", "topic": "squad.images"})
        self.assertTrue(again.get("already_loaded"))
        self.assertFalse(again["instructions"])

    def test_the_catalogue_lists_every_skill_with_its_directions(self):
        catalogue = {row["id"]: row for row in skill_catalog.skill_metadata()}
        self.assertEqual(set(catalogue), set(skill_catalog.PRIMARY_SKILLS))
        image = [row for row in catalogue["team-information"]["topics"] if row["id"] == "squad.images"]
        self.assertTrue(image and image[0]["description"])

    def test_panel_team_resolves_without_replacing_the_other_trees(self):
        self.assertEqual(skill_catalog.resolve_workflow("team"), "team")
        self.assertEqual(skill_catalog.resolve_workflow("team-maintenance"), "team_refresh")
        self.assertEqual(skill_catalog.resolve_workflow("team-information"), "team_maintenance")
        self.assertEqual(skill_catalog.resolve_workflow("team_refresh"), "team_refresh")
        with self.assertRaises(ValueError):
            skill_catalog.resolve_workflow("team", "team_refresh")
        with self.assertRaises(ValueError):
            skill_catalog.resolve_topic("team", "roster")
        with self.assertRaises(ValueError):
            skill_catalog.resolve_topic("team-information", "club.crest")
        with self.assertRaises(ValueError):
            skill_catalog.resolve_topic("team-maintenance", "stats.charts")
        panel = agent.load_skill("team")
        refresh = agent.load_skill("team_refresh")
        self.assertTrue(panel.strip())
        self.assertNotEqual(panel, refresh)
        self.assertIn("club.crest", panel)
        self.assertIn("stats.charts", panel)

    def test_panel_leaves_and_reason_codes_are_registered_in_order(self):
        self.assertEqual(skill_catalog.PANEL_LEAVES, (
            "club.crest", "squad.info", "squad.images", "schedule.standings", "stats.charts"))
        self.assertEqual(tuple(skill_catalog.topics("team")), skill_catalog.PANEL_LEAVES)
        catalogue = {row["id"]: row for row in skill_catalog.skill_metadata()}
        self.assertEqual([row["id"] for row in catalogue["team"]["topics"]],
                         list(skill_catalog.PANEL_LEAVES))
        self.assertEqual(skill_catalog.PANEL_REASON_CODES, {
            "MISSING": True,
            "STALE": True,
            "SOURCE_UNAVAILABLE": True,
            "RATE_LIMITED": True,
            "NAMESAKE_BLOCKED": False,
            "CREST_MISSING": False,
            "INCOMPLETE_SCHEDULE": True,
            "SAMPLE_INSUFFICIENT": False,
            "SCOPE_DENIED": False,
            "INTERRUPTED": False,
        })
        for leaf in skill_catalog.PANEL_LEAVES:
            declared = tool_registry.topic_declared("team", leaf)
            self.assertIsNotNone(declared, leaf)
            self.assertTrue(set(declared["capabilities"]) <= set(tool_registry.declared("team")))
            self.assertNotIn(tool_registry.NEWS, declared["capabilities"])
            self.assertNotIn(tool_registry.DELEGATE, declared["capabilities"])

    def test_panel_charts_narrow_and_scope_still_wins(self):
        parent = tool_registry.declared("team")
        charts = tool_registry.topic_declared("team", "stats.charts")["capabilities"]
        self.assertFalse(frozenset(charts) & frozenset({tool_registry.NEWS, tool_registry.EVIDENCE, tool_registry.DELEGATE}))
        self.assertTrue(set(charts) <= set(parent))
        store = FakeStore()
        local = LocalTools(store, store.new_run(skill="team"), store.settings(), agent.load_skill)
        before = names(local)
        self.assertIn("maintain_team_data", before)
        local.call("use_skill", {"skill": "team", "topic": "stats.charts"})
        after = names(local)
        for dropped in ("read_article", "discover_news", "research_news", "fetch_player_photos", "spawn_agent"):
            self.assertNotIn(dropped, after)
        # The chart leaf must not be handed the refresh tool. The parent keeps
        # it so it can delegate; the child allowlist is what removes it.
        child = LocalTools(store, store.new_run(
            skill="team", internal_agent=True,
            tool_allowlist=["get_team_data", "use_skill", "submit_answer"]), store.settings(), agent.load_skill)
        child.call("use_skill", {"skill": "team", "topic": "stats.charts"})
        offered = names(child)
        self.assertIn("get_team_data", offered)
        self.assertNotIn("maintain_team_data", offered)
        offline = LocalTools(store, store.new_run(skill="team", allow_network=False),
                             store.settings(), agent.load_skill)
        self.assertNotIn("maintain_team_data", names(offline))
        self.assertNotIn("fetch_player_photos", names(offline))

    def test_panel_acceptance_checklist_matches_the_catalog(self):
        text = (skill_catalog.SKILL_ROOT.parent / "docs" / "TEAM_PANEL_QA.md").read_text(encoding="utf-8")
        for leaf in skill_catalog.PANEL_LEAVES:
            self.assertIn(f"`{leaf}`", text)
        for code, retryable in skill_catalog.PANEL_REASON_CODES.items():
            flag = "true" if retryable else "false"
            self.assertIn(f"| {code} | {flag} |", text)
        self.assertIn("actionable_count == 0", text)
        self.assertIn("`INTERRUPTED`", text)


if __name__ == "__main__":
    unittest.main()
