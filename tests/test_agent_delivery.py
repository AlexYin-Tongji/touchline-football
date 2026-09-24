"""The product delivery protocol: read facts, validate, render once, stop."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from server import agent
from server.agent_tools import LocalTools, ToolError, answer_fact_limit
from tests.test_agent import FakeStore, tool_message


def submission(**changes):
    value = {"status": "complete", "answer": "目前值得关注的是中场的训练状态，是否能出场仍待确认。",
             "facts": [{"title": "训练状态仍有疑问", "detail": "来源称这名中场因伤暂未参加训练。", "evidence_ids": ["ev_injury"]}],
             "analysis": "人员选择可能受到影响，但现有资料尚不能确定下一场安排。",
             "unknowns": ["何时恢复合练", "是否进入比赛名单"]}
    value.update(changes)
    return value


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeStore()
        self.store.new_run()

    async def run_with(self, values):
        with patch.object(agent, "request_completion", new=AsyncMock(side_effect=values)) as mocked:
            await agent.run_agent(self.store, "run_one", asyncio.Event())
        return self.store.get("runs", "run_one"), mocked

    async def test_valid_submission_is_rendered_once_without_final_model_request(self):
        run, provider = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
                                             tool_message("submit_answer", submission())])
        self.assertEqual(provider.await_count, 2)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["answer_format"], "structured")
        self.assertEqual(run["evidence_ids"], ["ev_injury"])
        # A question is delivered as the prose answer, not reformatted into the
        # news template with fact headings and an impact section.
        self.assertEqual(run["answer"].count("目前值得关注的是中场的训练状态，是否能出场仍待确认。"), 1)
        self.assertNotIn("**训练状态仍有疑问**", run["answer"])
        self.assertNotIn("可能影响：", run["answer"])
        self.assertNotIn("submit_answer", run["answer"])
        self.assertNotIn("secret-example", json.dumps(run))

    async def test_invalid_reference_requires_corrected_submission(self):
        bad = submission(facts=[{"title": "伤情", "detail": "来源提到伤情。", "evidence_ids": ["ev_other_team"]}])
        run, provider = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
                                             tool_message("submit_answer", bad), tool_message("submit_answer", submission())])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(provider.await_count, 3)
        self.assertNotIn("ev_other_team", run["answer"])
        self.assertTrue(any(step["status"] == "failed" and step.get("tool") == "submit_answer" for step in run["steps"]))

    async def test_no_evidence_can_only_submit_explicit_insufficiency(self):
        value = submission(status="insufficient", answer="资料不足，暂不能确认最新伤情。", facts=[], analysis="", unknowns=["缺少可核对的最新医疗或球队声明"])
        run, provider = await self.run_with([tool_message("submit_answer", value)])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["delivery"]["status"], "insufficient")
        self.assertEqual(run["evidence_ids"], [])
        self.assertEqual(provider.await_count, 1)

    async def test_process_spill_text_gets_one_structured_delivery_retry(self):
        run, provider = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            {"content": "我已读完资料，联网预算已用尽。我将给出最终答复。来源说中场有伤。[ev_injury]"},
            tool_message("submit_answer", submission())])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(provider.await_count, 3)
        self.assertNotIn("我已读完", run["answer"])
        self.assertNotIn("预算", run["answer"])
        self.assertEqual(run["answer_format"], "structured")

    async def test_invalid_process_fields_are_not_rendered(self):
        # Process narration belongs in no delivered field, including the prose
        # answer, so this submission is rejected and the retry is what delivers.
        bad = submission(answer="我已读完两条原文，我将整理出结论。")
        run, provider = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", bad), tool_message("submit_answer", submission())])
        self.assertEqual(run["status"], "completed")
        self.assertNotIn("我将", run["answer"])
        self.assertEqual(provider.await_count, 3)

    def test_nested_limits_and_read_requirements_are_enforced(self):
        local = LocalTools(self.store, self.store.get("runs", "run_one"), self.store.settings())
        with self.assertRaises(ToolError):
            local.call("submit_answer", submission())
        local.call("read_evidence", {"evidence_id": "ev_injury"})
        bad_values = [submission(facts=[]), submission(summary="x" * 4001),
                      submission(facts=[{"title": "标题", "detail": "x" * 12001, "evidence_ids": ["ev_injury"]}]),
                      submission(facts=[{"title": "标题", "detail": "描述", "evidence_ids": "ev_injury"}]),
                      submission(facts=[{"title": "标题", "detail": "描述", "evidence_ids": ["ev_injury"], "private_thought": "bad"}]),
                      submission(unknowns=["x" * 2001])]
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(ToolError):
                local.call("submit_answer", value)
        self.assertIsNone(local.submission)

    def test_multiple_captures_of_same_url_are_one_source_document(self):
        first = self.store.get("evidence", "ev_injury")
        first["url"] = "https://example.test/news"
        self.store.put("evidence", first)
        second = dict(first, id="ev_updated")
        self.store.put("evidence", second)
        local = LocalTools(self.store, self.store.get("runs", "run_one"), self.store.settings())
        for eid in ("ev_injury", "ev_updated"):
            local.call("read_evidence", {"evidence_id": eid})
        value = submission()
        value["facts"][0]["evidence_ids"].append("ev_updated")
        local.call("submit_answer", value)
        self.assertEqual(local.submission["source_document_count"], 1)
        self.assertIn("同一篇来源原文", local.submission["answer"])
        self.assertEqual(local.submission["evidence_ids"], ["ev_injury", "ev_updated"])

    def test_explicit_two_item_limit_controls_structured_submission(self):
        for question in ("最多两条消息", "最多两点", "最多2条", "最多两项", "请给我两条重要新闻"):
            self.assertEqual(answer_fact_limit(question), 2)
        self.assertIsNone(answer_fact_limit("最近三场比赛有哪些变化？"))
        run = self.store.get("runs", "run_one")
        run["question"] = "最多两条具体的新进展"
        local = LocalTools(self.store, run, self.store.settings())
        local.call("read_evidence", {"evidence_id": "ev_injury"})
        value = submission()
        value["facts"] = [dict(value["facts"][0], title=title) for title in ("伤情", "训练", "名单")]
        with self.assertRaisesRegex(ToolError, "最多 2"):
            local.call("submit_answer", value)
        self.assertIsNone(local.submission)
        value["facts"] = value["facts"][:2]
        self.assertTrue(local.call("submit_answer", value)["submitted"])

    def test_default_delivery_accepts_more_than_three_facts_and_detailed_paragraphs(self):
        local = LocalTools(self.store, self.store.get("runs", "run_one"), self.store.settings())
        local.call("read_evidence", {"evidence_id": "ev_injury"})
        value = submission(answer="这是围绕用户问题展开的完整答复。" * 80,
                           analysis="人员选择需要结合训练恢复情况进行判断。" * 30 + "\n\n仍应等待正式名单。")
        value["facts"] = [dict(value["facts"][0], title=f"相关进展 {index}",
                               detail="来源说明了训练安排，应以当前可见资料为准。" * 15 + "\n\n后续应关注名单信息。")
                          for index in range(8)]
        result = local.call("submit_answer", value)
        self.assertEqual(result["facts_count"], 8)
        self.assertEqual(local.submission["structured_answer"], value)
        self.assertGreater(len(local.submission["answer"]), 1000)

    def test_only_user_requested_counts_are_limits(self):
        for question, expected in (("请给我三条新闻", 3), ("列出8项变化", 8), ("整理十五条消息", 15),
                                   ("最近三場比赛有哪些变化？", None), ("今天所有值得关注的消息", None),
                                   ("不要只要三条消息", None)):
            with self.subTest(question=question):
                self.assertEqual(answer_fact_limit(question), expected)

    def test_answer_cannot_upgrade_source_claim_to_verified_fact(self):
        local = LocalTools(self.store, self.store.get("runs", "run_one"), self.store.settings())
        local.call("read_evidence", {"evidence_id": "ev_injury"})
        value = submission(answer="值得关注的是媒体报道中的中场伤情，出场安排仍待进一步消息。")
        self.assertTrue(local.call("submit_answer", value)["submitted"])
