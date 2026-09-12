import copy
import unittest

from server.agent_tools import LocalTools
from server.presentation import present_events, readable_chinese
from server.team_profiles import _evidence_hash, archive_run_knowledge
from tests.test_agent import FakeStore


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.source = self.store.get("evidence", "ev_injury")
        self.source.update(title="Saka injury assessment", url="https://www.arsenal.com/news/saka",
                           text="Saka is being assessed ahead of training. The club has not announced when he will return.")
        self.store.put("evidence", self.source)
        self.event = {"id": "event_one", "mode": "live", "team_id": "arsenal", "kind": "injury",
                      "title": self.source["title"], "summary": self.source["text"], "evidence_ids": [self.source["id"]]}
        self.fact = {"title": "萨卡伤情仍待评估", "detail": "阿森纳表示萨卡仍在接受评估，尚未公布恢复训练的时间。", "evidence_ids": [self.source["id"]]}
        self.run = self.store.new_run(status="completed", read_evidence_ids=[self.source["id"]], evidence_ids=[self.source["id"]],
                                     structured_answer={"facts": [self.fact]}, finished_at="2026-09-09T10:05:00Z",
                                     verification={"status": "supported", "claims": [{"id": "fact_1", "text": self.fact["title"] + "：" + self.fact["detail"], "verdict": "supported"}]})

    def archive(self, run=None):
        run = run or self.run
        local = LocalTools(self.store, run, self.store.settings())
        local.call("read_evidence", {"evidence_id": self.source["id"]})
        archive_run_knowledge(self.store, run, read_cache=local.read_cache)

    def present(self, **kwargs):
        return present_events(self.store, kwargs.get("events", [self.event]),
                              kwargs.get("evidence", [self.store.get("evidence", self.source["id"])]),
                              kwargs.get("runs", [self.run]), spoiler_free=kwargs.get("spoiler_free", True))[0]

    def test_verified_knowledge_drives_card_without_rewriting_original(self):
        self.archive()
        result = self.present()
        self.assertEqual(result["display_title"], self.fact["title"])
        self.assertEqual(result["display_summary"], self.fact["detail"])
        self.assertEqual(result["summary_origin"], "assistant")
        self.assertEqual(result["display_status"], "supported")
        self.assertEqual(result["source_run_id"], self.run["id"])
        self.assertEqual(result["summary"], self.source["text"])
        self.assertNotIn("display_title", self.event)

    def test_source_text_or_metadata_change_invalidates_old_knowledge(self):
        self.archive()
        for field in ("text", "title", "url", "source_name", "published_at", "fetched_at"):
            changed = copy.deepcopy(self.source)
            changed[field] = str(changed.get(field) or "") + "changed"
            self.store.put("evidence", changed)
            self.assertEqual(self.present()["summary_origin"], "pending", field)
            self.store.put("evidence", self.source)

    def test_unversioned_historical_run_is_not_current_support(self):
        result = self.present()
        self.assertEqual(result["display_status"], "pending")
        self.assertEqual(result["display_title"], self.source["title"])
        self.assertIn("being assessed", result["display_summary"])
        self.assertEqual(result["display_language"], "original")
        self.assertEqual(result["report_note"], "中文报告待整理")

    def test_new_capture_of_same_page_supersedes_immutable_old_source(self):
        self.archive()
        newer = {**self.source, "id": "ev_new_capture", "fetched_at": "2026-09-10T10:00:00Z", "text": "The original update has been corrected. Saka has now returned to training."}
        self.store.put("evidence", newer)
        self.event["evidence_ids"].append(newer["id"])
        result = self.present(evidence=[self.source, newer])
        self.assertEqual(result["display_status"], "pending")

    def test_explicit_run_hashes_enable_fact_and_also_invalidate_on_source_change(self):
        self.run["evidence_hashes"] = {self.source["id"]: _evidence_hash(self.source)}
        self.assertEqual(self.present()["display_status"], "supported")
        self.store.put("evidence", {**self.source, "title": "Source title corrected"})
        self.assertEqual(self.present()["display_status"], "pending")

    def test_unverified_fact_is_visibly_weaker(self):
        self.run["verification"] = {"status": "not_requested", "claims": []}
        self.archive()
        self.assertEqual(self.present()["display_status"], "unverified")

    def test_knowledge_requires_visible_completed_run_and_delivered_source(self):
        self.archive()
        self.assertEqual(self.present(runs=[])["display_status"], "pending")
        for change in ({"status": "failed"}, {"evidence_ids": []}, {"team_id": "liverpool"}, {"mode": "demo"}, {"spoiler": True}):
            run = {**self.run, **change}
            self.assertEqual(self.present(runs=[run])["display_status"], "pending", str(change))
        self.assertEqual(self.present(evidence=[])["display_status"], "pending")

    def test_all_fact_sources_must_belong_to_this_event(self):
        self.run["structured_answer"]["facts"][0]["evidence_ids"].append("ev_transfer")
        self.run["read_evidence_ids"].append("ev_transfer")
        self.run["evidence_ids"].append("ev_transfer")
        self.run["evidence_hashes"] = {eid: _evidence_hash(self.store.get("evidence", eid)) for eid in self.run["evidence_ids"]}
        result = self.present(evidence=[self.source, self.store.get("evidence", "ev_transfer")])
        self.assertEqual(result["display_status"], "pending")

    def test_conflicting_fact_is_not_displayed_even_if_run_overall_supported(self):
        self.run["evidence_hashes"] = {self.source["id"]: _evidence_hash(self.source)}
        self.run["verification"]["claims"][0]["verdict"] = "conflicting"
        self.assertEqual(self.present()["display_status"], "pending")

    def test_pending_report_shows_actual_original_instead_of_fake_translation(self):
        self.event.update(title="球队新闻：Saka injury assessment and training update",
                          summary="来源摘要：Saka is being assessed ahead of training. The club has not announced when he will return.")
        result = self.present()
        self.assertFalse(readable_chinese(self.event["summary"]))
        self.assertEqual(result["display_title"], self.source["title"])
        self.assertIn("training", result["display_summary"])
        self.assertNotIn("相关报道已收录", result["display_summary"])
        self.assertEqual(result["display_language"], "original")

    def test_chinese_source_excerpt_is_short_and_not_a_full_article_dump(self):
        self.event.update(title="萨卡恢复训练仍待评估", summary="俱乐部表示萨卡仍在接受评估。" + "主教练会在接下来的训练中观察他的恢复情况。" * 20)
        result = self.present()
        self.assertEqual(result["summary_origin"], "source")
        self.assertEqual(result["display_status"], "unverified")
        self.assertEqual(result["display_summary"], "俱乐部表示萨卡仍在接受评估。")
        self.assertLess(len(result["display_summary"]), 120)

    def test_no_visible_source_does_not_republish_chinese_event_body(self):
        self.event.update(title="不可见的原文标题", summary="不可见的具体伤情细节。")
        result = self.present(evidence=[])
        self.assertEqual(result["display_title"], "暂无可读来源")
        self.assertEqual(result["display_summary"], "")


if __name__ == "__main__":
    unittest.main()
