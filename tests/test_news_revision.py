"""Targeted news edits preserve the manuscript and actual-read citation scope."""
import copy
import json
import unittest

from server.agent_tools import LocalTools, ToolError, tool_definitions
from tests.test_agent import FakeStore, tool_message
from tests.test_news_reports import manuscript


class NewsRevisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeStore()
        self.run = self.store.new_run(skill="news_report", report_id="report_one", input_evidence_ids=["ev_injury"], allow_network=False)
        self.local = LocalTools(self.store, self.run, self.store.settings())
        self.local.call("read_evidence", {"evidence_id": "ev_injury"})

    def repair(self):
        self.local.call("submit_answer", manuscript())
        self.local.news_draft = copy.deepcopy(self.local.submission["structured_answer"])
        self.local.submission = None
        return copy.deepcopy(self.local.news_draft)

    def fact(self, **changes):
        return {**copy.deepcopy(manuscript()["facts"][0]), **changes}

    def test_tool_is_only_advertised_and_executable_during_news_repair(self):
        self.assertIsNone(self.local.news_draft)
        self.assertNotIn("revise_news", {t["function"]["name"] for t in self.local.available_tools()})
        with self.assertRaises(ToolError):
            self.local.call("revise_news", {"summary": "修订后的导语明确说明训练安排。"})
        self.repair()
        self.assertIn("revise_news", {t["function"]["name"] for t in self.local.available_tools()})
        ordinary = LocalTools(self.store, self.store.new_run(skill="ask"), self.store.settings())
        ordinary.news_draft = copy.deepcopy(self.local.news_draft)
        self.assertNotIn("revise_news", {t["function"]["name"] for t in ordinary.available_tools()})
        with self.assertRaises(ToolError):
            ordinary.call("revise_news", {"summary": "修订后的导语明确说明训练安排。"})

    def test_replacement_preserves_every_untouched_paragraph_and_original_draft(self):
        draft = self.repair()
        replacement = self.fact(title="修订训练安排", detail="医疗更新显示这名中场因伤未能参加训练，具体恢复安排尚未公布，后续消息仍需依据俱乐部公开资料说明。")
        result = self.local.call("revise_news", {"replacements": [{"index": 1, "fact": replacement}]})
        current = self.local.submission["structured_answer"]
        self.assertTrue(result["submitted"])
        self.assertEqual(current["facts"][0], draft["facts"][0])
        self.assertEqual(current["facts"][2], draft["facts"][2])
        self.assertEqual(current["facts"][1]["detail"], replacement["detail"])
        for key in ("title", "summary", "analysis", "unknowns"):
            self.assertEqual(current[key], draft[key])
        self.assertEqual(self.local.news_draft, draft)

    def test_appended_tail_preserves_substantive_conclusion_and_all_prior_sections(self):
        source = self.store.get("evidence", "ev_injury")
        tail = "The closing statement promises continued support for the family and a permanent place in the club's history."
        source["text"] += "\n\n" + tail
        self.store.put("evidence", source)
        self.local.call("read_evidence", {"evidence_id": "ev_injury"})
        draft = self.repair()
        pid = next(p["id"] for p in self.local.read_cache["ev_injury"]["passages"] if p["text"] == tail)
        ending = self.fact(title="结尾保留纪念主旨", detail="声明最后承诺会继续向家人提供支持，并强调这位成员将永远在俱乐部历史中占有一席之地，这也是整篇纪念文字的实质结论。",
                           quotes=[{"evidence_id": "ev_injury", "passage_id": pid}])
        result = self.local.call("revise_news", {"append_facts": [ending]})
        current = self.local.submission["structured_answer"]
        self.assertEqual(result["facts_count"], 4)
        self.assertEqual(current["facts"][:-1], draft["facts"])
        self.assertEqual(current["facts"][-1]["detail"], ending["detail"])
        self.assertEqual(current["facts"][-1]["quotes"][0]["quote"], tail)

    def test_invalid_indexes_repetition_deletions_and_no_changes_are_rejected(self):
        draft = self.repair()
        fact = self.fact(title="新的有效段落")
        for args in ({}, {"summary": draft["summary"]}, {"replacements": []}, {"append_facts": []},
                     {"replacements": [{"index": 0, "fact": manuscript()["facts"][0]}]},
                     {"replacements": [{"index": -1, "fact": fact}]}, {"replacements": [{"index": True, "fact": fact}]},
                     {"replacements": [{"index": 3, "fact": fact}]},
                     {"replacements": [{"index": 0, "fact": fact}, {"index": 0, "fact": fact}]},
                     {"delete_indexes": [1]}, {"replacements": [{"index": 1, "fact": None}]}):
            with self.subTest(args=args), self.assertRaises(ToolError):
                self.local.call("revise_news", args)
            self.assertEqual(self.local.news_draft, draft)
            self.assertIsNone(self.local.submission)

    def test_forged_unread_or_changed_citations_never_replace_submission(self):
        draft = self.repair()
        for fact in (self.fact(title="无效片段", quotes=[{"evidence_id": "ev_injury", "passage_id": "p_forged"}]),
                     self.fact(title="其他来源", evidence_ids=["ev_transfer"], quotes=[{"evidence_id": "ev_transfer", "passage_id": "p_forged"}])):
            with self.subTest(fact=fact), self.assertRaises(ToolError):
                self.local.call("revise_news", {"append_facts": [fact]})
            self.assertIsNone(self.local.submission)
        self.store.put("evidence", {**self.store.get("evidence", "ev_injury"), "text": "The source was corrected."})
        with self.assertRaises(ToolError):
            self.local.call("revise_news", {"summary": "更完整的中文导语，保留原有段落。"})
        self.assertEqual(self.local.news_draft, draft)
        self.assertIsNone(self.local.submission)

    async def test_large_revision_uses_delivery_budget_in_sync_and_async_dispatch(self):
        self.repair()
        detail = "这段中文稿件保留原文明确提供的训练状态和后续安排，并且把报道中的条件说明清楚。" * 120
        args = {"replacements": [{"index": 1, "fact": self.fact(title="较长段落的局部修訂", detail=detail)}]}
        self.assertGreater(len(json.dumps(args, ensure_ascii=False)), 3000)
        call = tool_message("revise_news", args)["tool_calls"][0]
        for result in (self.local.run_tool_call(call), await self.local.arun_tool_call(call)):
            self.assertTrue(result.get("submitted"), result)
            self.assertEqual(self.local.submission["structured_answer"]["facts"][1]["detail"], detail)
        oversized = tool_message("revise_news", {"summary": "字" * 65000})["tool_calls"][0]
        self.assertIn("error", await self.local.arun_tool_call(oversized))

    def test_revision_schema_reuses_news_fact_contract(self):
        tools = {d["name"]: d for d in tool_definitions("news_report")}
        fact = tools["submit_answer"]["parameters"]["properties"]["facts"]["items"]
        fields = tools["revise_news"]["parameters"]["properties"]
        self.assertEqual(fields["replacements"]["items"]["properties"]["fact"], fact)
        self.assertEqual(fields["append_facts"]["items"], fact)
        self.assertNotIn("quote", fact["properties"]["quotes"]["items"]["properties"])


if __name__ == "__main__":
    unittest.main()
