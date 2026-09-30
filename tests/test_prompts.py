"""The prompt is prose, so nothing else stops it going stale.

Several statements in the previous version described behaviour that had already
changed: it still promised a verification pass that could withhold an answer, and
still named a reader-preferences tool that had since been folded into another.
These tests check the prompt against what the code actually does, which is the
only thing that makes the drift visible.
"""
from pathlib import Path
import tempfile
import unittest

from server import prompts
from server.agent_tools import LocalTools, TOOL_DEFINITIONS
from server.storage import Store


class PromptCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "app.sqlite3")
        self.addCleanup(self.store.close)
        self.store.put("teams", {"id": "arsenal", "name": "阿森纳", "english_name": "Arsenal"})
        self.run = {"id": "run_one", "mode": "live", "team_id": "arsenal", "skill": "ask",
                    "question": "伤病消息确认了吗？", "conversation_id": "c1", "status": "running"}
        self.local = LocalTools(self.store, self.run, self.store.settings())

    def prompt(self, skill="ask"):
        return prompts.system_prompt(self.run, self.store.settings(), self.local, skill)


class DriftTests(PromptCase):
    """Every claim the prompt makes must still be true of the code."""

    def test_the_prompt_does_not_promise_an_answer_can_be_withheld(self):
        text = self.prompt()
        for stale in ("至多允许一次补证据或修复", "未通过核对", "会被移除未通过核对", "核对未通过"):
            self.assertNotIn(stale, text)

    def test_the_prompt_describes_labelling_rather_than_a_gate(self):
        self.assertIn("标注", self.prompt())

    def test_the_prompt_names_tools_that_exist(self):
        available = {tool["name"] for tool in TOOL_DEFINITIONS}
        text = self.prompt()
        for retired in ("reader_profile", "team_context", "source_overview"):
            self.assertNotIn(retired, text)
        self.assertIn("context_pack", text)

    def test_the_prompt_does_not_offer_background_maintenance(self):
        # These are callable but not advertised, so telling the model to use
        # one would send it at a tool it never receives.
        text = self.prompt()
        self.assertNotIn("maintain_team_data", text)

    def test_the_prompt_still_states_the_citation_rule_the_code_enforces(self):
        text = self.prompt()
        self.assertIn("引用", text)
        self.assertIn("实际读到", text)


class BoundaryTests(PromptCase):
    def test_the_prompt_carries_the_task_scope(self):
        text = self.prompt()
        self.assertIn("arsenal", text)
        self.assertIn("ask", text)

    def test_the_spoiler_boundary_is_stated(self):
        self.assertIn("避剧透", self.prompt())

    def test_a_subtask_is_told_its_result_is_a_candidate(self):
        self.run["internal_agent"] = True
        self.assertIn("候选", self.prompt())

    def test_a_subtask_is_not_told_its_result_is_a_candidate_when_it_is_not_one(self):
        self.assertNotIn("候选结果", self.prompt())

    def test_the_editorial_brief_is_used_for_a_report(self):
        self.run["skill"] = "news_report"
        text = self.prompt("news_report")
        self.assertIn("中文足球新闻编辑", text)

    def test_the_conversational_brief_is_used_for_a_question(self):
        self.assertNotIn("中文足球新闻编辑", self.prompt())

    def test_a_declared_fact_count_is_carried_into_the_prompt(self):
        # The phrase the extractor recognises; see agent_tools.answer_fact_limit.
        self.run["question"] = "请给我 3 条最新消息"
        self.assertIn("最多 3 项", self.prompt())

    def test_an_unstated_count_leaves_the_model_to_choose(self):
        self.assertIn("未限定消息数量", self.prompt())


class HistoryTests(PromptCase):
    def test_history_is_labelled_as_background_not_evidence(self):
        self.store.put("runs", {**self.run, "id": "older", "status": "completed",
                                "question": "上一问", "answer": "上一答"})
        messages = prompts.history_messages(self.local)
        # The label is stated once for the block rather than repeated before
        # every prior answer, so the transcript carries no fixed caveat sentence
        # for the model to imitate.
        self.assertEqual(messages[0]["role"], "user")
        self.assertIn("不作为本次证据", messages[0]["content"])
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[2]["role"], "assistant")
        self.assertEqual(messages[2]["content"], "上一答")

    def test_history_is_empty_without_a_conversation(self):
        self.run.pop("conversation_id")
        local = LocalTools(self.store, self.run, self.store.settings())
        self.assertEqual(prompts.history_messages(local), [])


class DemoAnswerTests(unittest.TestCase):
    """The offline path must be labelled as fiction, always."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "app.sqlite3")
        self.addCleanup(self.store.close)
        self.run = {"id": "r", "mode": "demo", "team_id": "arsenal", "skill": "ask", "question": "问题"}

    def test_an_empty_result_says_so(self):
        text = prompts.demo_answer(self.run, None, [], "standard")
        self.assertIn("虚构示例", text)
        self.assertIn("暂未找到", text)

    def test_a_demo_answer_is_always_marked_as_fiction(self):
        text = prompts.demo_answer(self.run, None, [{"id": "ev_1", "title": "示例消息"}], "standard")
        self.assertTrue(text.startswith("虚构示例"))

    def test_a_non_chinese_title_becomes_a_neutral_placeholder(self):
        text = prompts.demo_answer(self.run, None, [{"id": "ev_1", "title": "Some English headline"}], "standard")
        self.assertIn("已收录一条相关示例资料", text)


if __name__ == "__main__":
    unittest.main()
