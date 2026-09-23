"""Omission-check boundaries with a labelled model stub, never paid requests."""
import asyncio
import copy
import hashlib
import json
import unittest
from unittest.mock import AsyncMock, patch

from server import news_coverage
from server.agent_tools import LocalTools
from tests.test_agent import FakeStore, tool_message
from tests.test_agent_delivery import submission


def coverage_reply(omissions=None):
    return tool_message("report_news_coverage", {"omissions": omissions or []})


class NewsCoverageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeStore()
        self.run = self.store.new_run(skill="news_report", question="完整整理这篇报道，包括所有重要内容。",
                                      input_evidence_ids=["ev_injury"])
        self.local = LocalTools(self.store, self.run, self.store.settings())
        self.config = {**self.store.settings()["llm"], "_session_id": "article_draft", "thinking_enabled": True}
        self.draft = submission()

    def read(self, text=None, *, eid="ev_injury", all_pages=True, **changes):
        source = self.store.get("evidence", eid)
        if text is not None:
            source["text"] = text
        source.update(content_status="full_text", **changes)
        self.store.put("evidence", source)
        page = self.local.call("read_evidence", {"evidence_id": eid})
        while all_pages and page.get("next_offset") is not None:
            page = self.local.call("read_evidence", {"evidence_id": eid, "offset": page["next_offset"]})
        return self.local.read_cache[eid]

    async def test_long_article_tail_is_sent_and_a_real_omission_is_reported(self):
        beginning = "Premier League review covers the title race and European qualification."
        middle = "\n\n".join(f"Section {n}: The author describes the clubs' plans and their remaining fixtures in detail. " * 3 for n in range(65))
        ending = "The relegation battle is the final major conclusion: three clubs face a points deduction unless the appeal succeeds by 30 September."
        text = beginning + "\n\n" + middle + "\n\n" + ending
        cache = self.read(text)
        self.assertGreater(len(text), 16169)
        tail = next(p for p in cache["passages"] if ending in p["text"])
        async def checker(config, messages, **options):
            payload = json.loads(messages[1]["content"])
            self.assertEqual(payload["sources"][0]["text"], text)
            self.assertIn(tail, payload["sources"][0]["passages"])
            self.assertEqual(payload["question"], self.run["question"])
            self.assertEqual(payload["team_id"], "arsenal")
            self.assertEqual(options["tool_choice"]["function"]["name"], "report_news_coverage")
            self.assertFalse(config["thinking_enabled"])
            self.assertNotEqual(config["_session_id"], self.config["_session_id"])
            return coverage_reply([{"evidence_id": "ev_injury", "passage_id": tail["id"], "reason": "遗漏文末保级竞争、扣分条件及申诉截止日。"}])
        result = await news_coverage.assess_news_coverage(self.local, self.draft, self.config, requester=checker)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["omissions"][0]["passage_id"], tail["id"])
        self.assertEqual(result["protocol_repairs"], 0)

    async def test_empty_omissions_complete_without_mutating_source_draft_or_configuration(self):
        self.read()
        before = copy.deepcopy((self.store.data, self.local.read_cache, self.draft, self.config))
        request = AsyncMock(return_value=coverage_reply())
        result = await news_coverage.assess_news_coverage(self.local, self.draft, self.config, requester=request)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["omissions"], [])
        self.assertEqual(result["scope"], "actually_read_visible_text")
        self.assertEqual((self.store.data, self.local.read_cache, self.draft, self.config), before)
        self.assertEqual(request.await_count, 1)

    async def test_entirely_uncited_read_source_is_still_checked(self):
        self.read()
        cache = self.read("The club closes its tribute by pledging lifelong support to the player's family.", eid="ev_transfer")
        request = AsyncMock(return_value=coverage_reply([{"evidence_id": "ev_transfer", "passage_id": cache["passages"][0]["id"], "reason": "整篇悼文的结尾承诺未被稿件涵盖。"}]))
        result = await news_coverage.assess_news_coverage(self.local, self.draft, self.config, requester=request)
        self.assertEqual(result["status"], "incomplete")
        payload = json.loads(request.await_args.args[1][1]["content"])
        self.assertEqual({s["id"] for s in payload["sources"]}, {"ev_injury", "ev_transfer"})

    async def test_forged_wrong_source_and_duplicate_passage_results_fail_closed(self):
        cache = self.read()
        other = self.read(eid="ev_transfer")
        valid = {"evidence_id": "ev_injury", "passage_id": cache["passages"][0]["id"], "reason": "正文遗漏核心条件。"}
        for bad in ([{**valid, "passage_id": "p_forged"}], [{**valid, "passage_id": other["passages"][0]["id"]}],
                    [{**valid, "evidence_id": "ev_other_team"}], [valid, valid], [{**valid, "reason": ""}], [{**valid, "extra": True}]):
            with self.subTest(bad=bad):
                request = AsyncMock(return_value=coverage_reply(bad))
                result = await news_coverage.assess_news_coverage(self.local, self.draft, self.config, requester=request)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["omissions"], [])
                self.assertEqual(request.await_count, 2)

    async def test_one_format_retry_uses_frozen_sources_and_an_independent_session(self):
        self.read()
        bad = {"role": "assistant", "content": "wrong format", "tool_calls": []}
        request = AsyncMock(side_effect=[bad, coverage_reply()])
        result = await news_coverage.assess_news_coverage(self.local, self.draft, self.config, requester=request)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["protocol_repairs"], 1)
        first, second = request.await_args_list
        self.assertEqual(first.args[1][1], second.args[1][1])
        self.assertNotEqual(first.args[0]["_session_id"], second.args[0]["_session_id"])
        self.assertNotIn("wrong format", json.dumps(second.args[1]))

    async def test_changed_or_hidden_source_before_request_never_calls_checker(self):
        for changes in ({"text": "Updated source."}, {"private": True}, {"visibility": "hidden"}):
            with self.subTest(changes=changes):
                self.setUp()
                self.read()
                self.store.put("evidence", {**self.store.get("evidence", "ev_injury"), **changes})
                request = AsyncMock(return_value=coverage_reply())
                result = await news_coverage.assess_news_coverage(self.local, self.draft, self.config, requester=request)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["reason"], "source_changed")
                request.assert_not_called()

    async def test_source_change_during_response_invalidates_empty_result_and_format_retry(self):
        for response in (coverage_reply(), {"tool_calls": []}):
            with self.subTest(response=response):
                self.setUp()
                self.read()
                async def checker(*args, **kwargs):
                    self.store.put("evidence", {**self.store.get("evidence", "ev_injury"), "text": "Corrected article."})
                    return response
                request = AsyncMock(side_effect=checker)
                result = await news_coverage.assess_news_coverage(self.local, self.draft, self.config, requester=request)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["reason"], "source_changed")
                self.assertEqual(request.await_count, 1)

    async def test_private_source_during_check_never_returns_old_title_url_or_body(self):
        cache = self.read(title="PrivateSourceTitle", url="https://example.test/private-article")
        async def checker(*args, **kwargs):
            self.store.put("evidence", {**self.store.get("evidence", "ev_injury"), "private": True})
            return coverage_reply()
        result = await news_coverage.assess_news_coverage(self.local, self.draft, self.config, requester=checker)
        self.assertEqual(result["reason"], "source_changed")
        public = json.dumps(result)
        for private in (cache["title"], cache["url"], cache["text"]):
            self.assertNotIn(private, public)

    async def test_unread_tail_or_selected_source_cannot_be_declared_complete(self):
        self.read("First paragraph has valid material. " * 600, all_pages=False)
        request = AsyncMock(return_value=coverage_reply())
        result = await news_coverage.assess_news_coverage(self.local, self.draft, self.config, requester=request)
        self.assertEqual(result["reason"], "incomplete_read")
        request.assert_not_called()
        self.read()
        self.run["input_evidence_ids"].append("ev_transfer")
        result = await news_coverage.assess_news_coverage(self.local, self.draft, self.config, requester=request)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "incomplete_read")

    async def test_expired_shared_body_is_valid_only_with_same_durable_hash(self):
        cache = self.read()
        source = self.store.get("evidence", "ev_injury")
        source.update(body_hash=hashlib.sha256(source["text"].encode()).hexdigest(), text="", body_storage="transient")
        self.store.put("evidence", source)
        request = AsyncMock(return_value=coverage_reply())
        result = await news_coverage.assess_news_coverage(self.local, self.draft, self.config, requester=request)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(json.loads(request.await_args.args[1][1]["content"])["sources"][0]["text"], cache["text"])

    async def test_illegal_cached_passage_never_reaches_checker(self):
        cache = self.read()
        cache["passages"].append({"id": "p_unread", "text": "This was never read."})
        request = AsyncMock(return_value=coverage_reply())
        result = await news_coverage.assess_news_coverage(self.local, self.draft, self.config, requester=request)
        self.assertEqual(result["status"], "unavailable")
        request.assert_not_called()

    async def test_cancellation_propagates_and_timeout_is_unavailable(self):
        self.read()
        started = asyncio.Event()
        async def checker(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        before = copy.deepcopy(self.store.data)
        task = asyncio.create_task(news_coverage.assess_news_coverage(self.local, self.draft, self.config, requester=checker))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        result = await news_coverage.assess_news_coverage(self.local, self.draft, self.config, timeout=.01, requester=checker)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(self.store.data, before)

    async def test_missing_injected_requester_never_calls_provider(self):
        self.read()
        with patch("server.provider.request_completion", new=AsyncMock()) as provider:
            result = await news_coverage.assess_news_coverage(self.local, self.draft, self.config)
        self.assertEqual(result["status"], "unavailable")
        provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
