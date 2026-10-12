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


if __name__ == "__main__":
    unittest.main()
