"""Lossless checker-input compaction and explicit source-reading boundaries."""
import copy
import json
import unittest
from unittest.mock import AsyncMock

from server import verification
from server.agent_tools import LocalTools
from tests.test_agent import FakeStore, tool_message
from tests.test_verification import quoted_submission


class VerificationInputTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.local = LocalTools(self.store, self.store.new_run(), self.store.settings())

    def read(self, text=None, **arguments):
        if text is not None:
            source = self.store.get("evidence", "ev_injury")
            self.store.put("evidence", {**source, "text": text})
        return self.local.call("read_evidence", {"evidence_id": "ev_injury", **arguments})

    def test_reused_quote_is_sent_once_per_claim_without_losing_any_fact(self):
        self.read()
        value = quoted_submission()
        fact = value["facts"][0]
        value["facts"] = [{**copy.deepcopy(fact), "title": f"已读消息{i}"} for i in range(20)]
        claims, payload = verification.prepare_claims(self.local, value)
        self.assertEqual(len([c for c in payload["claims"] if c["id"].startswith("fact_")]), 20)
        self.assertEqual([len(c["quotes"]) for c in payload["claims"]], [1] * len(payload["claims"]))
        self.assertTrue(all(c["quotes"][0] == fact["quotes"][0] for c in claims))

    def test_overlapping_context_windows_merge_without_changing_a_character(self):
        quote_a = "The midfielder is NOT ready for training."
        quote_b = "The medical staff require another assessment."
        text = "Lead material. " * 70 + quote_a + " " + "Middle material. " * 12 + quote_b + " Tail material." * 100
        self.read(text)
        value = quoted_submission()
        value["facts"][0]["quotes"] = [{"evidence_id": "ev_injury", "quote": q} for q in (quote_a, quote_b)]
        _, payload = verification.prepare_claims(self.local, value)
        contexts = payload["sources"][0]["passages"]
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0], text[text.index(quote_a) - 800:text.index(quote_b) + len(quote_b) + 800])
        self.assertIn("NOT ready", contexts[0])
        self.assertEqual([q["quote"] for q in payload["claims"][0]["quotes"]], [quote_a, quote_b])

    def test_context_merge_preserves_unread_gap_and_never_adds_hidden_text(self):
        first = "The midfielder is unavailable for training."
        last = "The medical staff will publish another update."
        text = first + " First section. " * 100 + "INVISIBLE MIDDLE SECTION" + " Second section. " * 100 + last
        self.read(text, length=600)
        self.read(offset=len(text) - 600, length=600)
        value = quoted_submission()
        value["facts"][0]["quotes"] = [{"evidence_id": "ev_injury", "quote": q} for q in (first, last)]
        _, payload = verification.prepare_claims(self.local, value)
        supplied = "\n".join(payload["sources"][0]["passages"])
        self.assertNotIn("INVISIBLE MIDDLE SECTION", supplied)
        self.assertIn("未读取的正文区间", supplied)
        self.assertIn(first, supplied)
        self.assertIn(last, supplied)

    def test_checker_receives_actual_read_scope_separate_from_local_context_windows(self):
        text = "Medical update: the midfielder has an injury and is unavailable for training. " + "Later material. " * 250
        self.read(text, length=1200)
        value = quoted_submission()
        _, payload = verification.prepare_claims(self.local, value)
        source = payload["sources"][0]
        self.assertEqual(source.get("content_status"), self.local.read_cache["ev_injury"]["content_status"])
        self.assertEqual(source.get("read_ranges"), [[0, 1200]])
        self.assertEqual(source.get("total_characters"), len(text))
        self.assertTrue(source.get("truncated"))
        self.assertFalse(source.get("checker_context_complete"))
        self.assertEqual(source["published_at"], "2026-08-31T09:00:00Z")
        self.assertEqual(source["fetched_at"], "2026-08-31T10:00:00Z")

    def test_checker_distinguishes_complete_reading_from_excerpted_checker_input(self):
        text = "Medical update: the midfielder has an injury and is unavailable for training. " + "Additional body material. " * 180
        self.read(text)
        _, payload = verification.prepare_claims(self.local, quoted_submission())
        source = payload["sources"][0]
        self.assertFalse(source.get("truncated"))
        self.assertIs(source.get("checker_context_complete"), False)
        self.assertLess(sum(map(len, source["passages"])), len(text))

    def test_partial_second_source_does_not_reject_complete_first_source_fact(self):
        self.read()
        second = self.store.get("evidence", "ev_transfer")
        original_quote = second["text"]
        self.store.put("evidence", {**second, "text": original_quote + " Later detail." * 200})
        self.local.call("read_evidence", {"evidence_id": "ev_transfer", "length": 500})
        value = quoted_submission()
        value["facts"][0]["detail"] = "报道未提供恢复日期。"
        value["facts"].append({"title": "合同日期", "detail": "报道未提供合同到期日期。",
                               "evidence_ids": ["ev_transfer"],
                               "quotes": [{"evidence_id": "ev_transfer", "quote": original_quote}]})
        value["summary"] = "报道未提供恢复日期。"
        value["analysis"] = "原文未提供完整时间表。"
        value["unknowns"] = ["报道未提供合同到期日期。"]
        claims, payload = verification.prepare_claims(self.local, value)
        by_id = {claim["id"]: claim for claim in claims}
        self.assertEqual(by_id["fact_1"]["verdict"], "uncertain")
        self.assertEqual(by_id["fact_2"]["verdict"], "unsupported")
        self.assertEqual([c["id"] for c in payload["claims"]], ["fact_1"])
        self.assertTrue(all(by_id[name]["verdict"] == "unsupported" for name in ("summary", "analysis", "unknowns")))


class VerificationCompactRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_preserves_exact_claim_obligations_and_annotations(self):
        store = FakeStore()
        local = LocalTools(store, store.new_run(), store.settings())
        local.call("read_evidence", {"evidence_id": "ev_injury"})
        value = quoted_submission()
        expected = ["fact_1", "summary", "analysis", "unknowns"]
        async def checker(config, messages, **options):
            payload = json.loads(messages[-1]["content"])
            self.assertEqual([row["id"] for row in payload["claims"]], expected)
            self.assertTrue(all(row["quotes"] for row in payload["claims"]))
            return tool_message("report_source_support", {"claims": [
                {"id": name, "verdict": "unsupported" if name == "analysis" else "supported", "reason": "来源已核对"}
                for name in expected]})
        result = await verification.verify_submission(local, value, store.settings()["llm"], requester=checker)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(next(c["verdict"] for c in result["claims"] if c["id"] == "analysis"), "unsupported")
        annotated = verification.annotations(value, result)
        self.assertEqual(annotated["prose"]["analysis"]["verdict"], "unsupported")

    async def test_locally_rejected_quote_never_reaches_checker_after_compaction(self):
        store = FakeStore()
        local = LocalTools(store, store.new_run(), store.settings())
        local.call("read_evidence", {"evidence_id": "ev_injury"})
        value = quoted_submission()
        value["facts"][0]["quotes"][0]["quote"] = "Invented assertion not present in any source."
        requester = AsyncMock()
        result = await verification.verify_submission(local, value, store.settings()["llm"], requester=requester)
        self.assertEqual(result["status"], "insufficient")
        requester.assert_not_awaited()
