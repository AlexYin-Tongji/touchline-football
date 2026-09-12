"""Readable source coverage, bounded RSS follow-through and literal page scope."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server import agent, ingestion, verification
from server.agent_tools import LocalTools, ToolError
from tests.test_agent import FakeStore
from tests.test_verification import quoted_submission


PARAGRAPH = "The manager discussed the training programme and explained that the squad would prepare together before the upcoming fixture. Fitness will be assessed at the next session, and the club has not announced a matchday squad."
ARTICLE = ("<html><title>Training programme</title><article><p>" + PARAGRAPH + "</p><p>" + PARAGRAPH + " Further information is expected later.</p><p>" + PARAGRAPH + "</p></article></html>").encode()


class ContentCoverageTests(unittest.TestCase):
    def test_article_body_is_preserved_as_paragraphs_without_related_cards(self):
        body = ARTICLE.replace(b"</article>", b"<aside>Unrelated menu text</aside><div class='related-content'>Read this unrelated story</div></article>")
        value = ingestion.extract_html(body, "https://example.com/article", "Test")[0]
        detail = ingestion.content_details(value)
        self.assertEqual(value["content_status"], "full_text")
        self.assertNotIn("Unrelated", value["text"])
        self.assertNotIn("unrelated story", value["text"])
        self.assertGreaterEqual(detail["paragraph_count"], 3)
        self.assertGreater(detail["word_count"], 50)
        self.assertTrue(all(row["text"] in value["text"] for row in detail["paragraphs"]))

    def test_short_gated_and_truncated_content_never_claim_complete_body(self):
        for body in (b"<article>A brief article with only a short paragraph about training.</article>",
                     ARTICLE.replace(b"</article>", b"<p>Subscribe to continue reading</p></article>"),
                     ("<article>" + PARAGRAPH * 200 + "</article>").encode()):
            value = ingestion.extract_html(body, "https://example.com/article", "Test")[0]
            self.assertEqual(value["content_status"], "partial")
        self.assertEqual(len(value["text"]), 30000)
        self.assertTrue(value["text_truncated"])

    def test_feed_summary_is_not_mislabelled_as_full_article(self):
        body = ("<rss><channel><item><title>Training</title><description>" + PARAGRAPH + "</description><link>https://example.com/article</link></item></channel></rss>").encode()
        item = ingestion.extract_feed(body, "https://example.com/feed", "Test")[0]
        self.assertEqual(item["content_status"], "summary")
        self.assertEqual(item["content_method"], "feed_summary")
        self.assertIsNone(item["published_at"])

    def test_atom_xhtml_preserves_paragraph_boundaries(self):
        body = ("<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>Training</title><content type='xhtml'><div xmlns='http://www.w3.org/1999/xhtml'><p>" + PARAGRAPH + "</p><p>" + PARAGRAPH + "</p></div></content></entry></feed>").encode()
        item = ingestion.extract_feed(body, "https://example.com/feed", "Test")[0]
        self.assertEqual(ingestion.content_details(item)["paragraph_count"], 2)
        self.assertEqual(item["content_status"], "partial")

    def test_redacted_details_derive_only_from_visible_text(self):
        original = {"title": "Training programme", "origin": "web", "spoiler": True,
                    "text": PARAGRAPH + "\nThe match ended 3-1 and the club won.\n" + PARAGRAPH,
                    "content_status": "full_text", "url": "https://example.com/training"}
        view = ingestion.safe_evidence_view(original)
        detail = ingestion.content_details(view)
        self.assertEqual(detail["content_status"], "partial")
        self.assertNotIn("3-1", json.dumps(detail))
        self.assertIn("3-1", original["text"])
        self.assertEqual(ingestion.content_details({"text": PARAGRAPH, "origin": "rss"})["content_status"], "summary")


class FeedFollowThroughTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = {"name": "Example", "url": "https://example.com/feed"}
        self.entry = {"title": "Training", "text": PARAGRAPH, "url": "https://example.com/article",
                      "published_at": "2026-09-10T09:00:00+00:00", "content_status": "summary"}

    async def test_summary_follows_original_and_keeps_feed_publication_date(self):
        with patch("server.ingestion.fetch_document", return_value=(ARTICLE, self.entry["url"], "text/html")) as fetch:
            entries, counts = await ingestion.follow_feed_articles([self.entry], self.source, self.source["url"])
        self.assertEqual(entries[0]["content_status"], "full_text")
        self.assertGreater(len(entries[0]["text"]), len(self.entry["text"]))
        self.assertEqual(entries[0]["published_at"], self.entry["published_at"])
        self.assertEqual(fetch.call_args.kwargs["allowed_hosts"], {"example.com"})
        self.assertEqual(counts["full_text"], 1)

    async def test_network_error_keeps_summary_without_claiming_body_was_read(self):
        with patch("server.ingestion.fetch_document", side_effect=ingestion.IngestionError("blocked")):
            entries, counts = await ingestion.follow_feed_articles([self.entry], self.source, self.source["url"])
        self.assertEqual(entries[0]["text"], self.entry["text"])
        self.assertEqual(entries[0]["content_status"], "summary")
        self.assertEqual(counts["summary_only"], 1)

    async def test_followups_are_bounded_and_external_or_private_links_are_not_fetched(self):
        entries = [{**self.entry, "url": "https://example.com/article/" + str(index)} for index in range(12)]
        with patch("server.ingestion.fetch_document", return_value=(ARTICLE, self.entry["url"], "text/html")) as fetch:
            result, counts = await ingestion.follow_feed_articles(entries, self.source, self.source["url"])
        self.assertEqual(fetch.call_count, 6)
        self.assertEqual(counts["followed"], 6)
        self.assertEqual(result[-1]["content_status"], "summary")
        for url in ("https://unrelated.example/article", "http://127.0.0.1/private", "http://169.254.169.254/latest/meta-data"):
            with patch("server.ingestion.fetch_document") as fetch:
                result, _ = await ingestion.follow_feed_articles([{**self.entry, "url": url}], self.source, self.source["url"])
                fetch.assert_not_called()
                self.assertEqual(result[0]["content_status"], "summary")

    async def test_redirect_destination_is_rechecked_even_after_transport_return(self):
        with patch("server.ingestion.fetch_document", return_value=(ARTICLE, "http://127.0.0.1/private", "text/html")):
            result, _ = await ingestion.follow_feed_articles([self.entry], self.source, self.source["url"])
        self.assertEqual(result[0]["text"], self.entry["text"])


class ReaderToolsTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.local = LocalTools(self.store, self.store.new_run(), self.store.settings(), agent.load_skill)

    def test_late_paragraph_can_be_located_then_read_without_granting_unread_middle(self):
        item = self.store.get("evidence", "ev_injury")
        item["text"] = "A" * 12000 + "\nUnseen middle statement.\n" + "B" * 3000 + "\nThe midfielder completed rehabilitation at the training ground."
        self.store.put("evidence", item)
        first = self.local.call("read_evidence", {"evidence_id": "ev_injury"})
        self.assertEqual(first["next_offset"], 12000)
        result = self.local.call("search_passages", {"evidence_id": "ev_injury", "query": "rehabilitation"})
        offset = result["results"][0]["offset"]
        self.assertGreater(offset, 12000)
        self.assertNotIn("completed rehabilitation", self.local.read_cache["ev_injury"]["text"])
        last = self.local.call("read_evidence", {"evidence_id": "ev_injury", "offset": offset})
        self.assertIsNone(last["next_offset"])
        cache = self.local.read_cache["ev_injury"]
        self.assertIn("completed rehabilitation", cache["text"])
        self.assertNotIn("Unseen middle statement", cache["text"])
        self.assertTrue(cache["truncated"])
        self.assertTrue(all(p in cache["passages"] for p in first["passages"]))
        value = quoted_submission()
        value["facts"][0]["quotes"] = [{"evidence_id": "ev_injury", "passage_id": last["passages"][0]["id"]}]
        self.assertIsNotNone(verification.prepare_claims(self.local, value)[1])
        value["facts"][0]["quotes"] = [{"evidence_id": "ev_injury", "quote": "Unseen middle statement."}]
        self.assertIsNone(verification.prepare_claims(self.local, value)[1])

    def test_search_passages_does_not_grant_citations_and_rechecks_scope(self):
        result = self.local.call("search_passages", {"query": "injury"})
        self.assertTrue(result["results"])
        self.assertFalse(self.local.read_ids)
        for eid in ("ev_other_team", "ev_result", "ev_demo"):
            with self.assertRaises(ToolError):
                self.local.call("search_passages", {"query": "match", "evidence_id": eid})
            with self.assertRaises(ToolError):
                self.local.call("read_evidence", {"evidence_id": eid, "offset": 0})

    def test_reading_adjacent_pages_covers_document_and_replacement_drops_old_quotes(self):
        item = self.store.get("evidence", "ev_injury")
        item["text"] = PARAGRAPH * 5
        self.store.put("evidence", item)
        first = self.local.call("read_evidence", {"evidence_id": item["id"], "length": 500})
        self.local.call("read_evidence", {"evidence_id": item["id"], "offset": first["next_offset"]})
        self.assertFalse(self.local.read_cache[item["id"]]["truncated"])
        item["text"] = "A completely replaced article with a different medical statement."
        self.store.put("evidence", item)
        self.assertFalse(self.local.cache_is_current(item["id"]))
        self.local.call("read_evidence", {"evidence_id": item["id"]})
        self.assertNotIn(PARAGRAPH, self.local.read_cache[item["id"]]["text"])

    def test_comparison_requires_read_sources_and_groups_tracking_urls(self):
        for eid, suffix in (("ev_injury", "?utm_source=a"), ("ev_transfer", "?utm_source=b")):
            item = self.store.get("evidence", eid)
            item.update(url="https://example.com/article" + suffix, text=PARAGRAPH)
            self.store.put("evidence", item)
        args = {"evidence_ids": ["ev_injury", "ev_transfer"]}
        with self.assertRaises(ToolError):
            self.local.call("compare_sources", args)
        for eid in args["evidence_ids"]:
            self.local.call("read_evidence", {"evidence_id": eid})
        result = self.local.call("compare_sources", args)
        self.assertEqual(result["document_count"], 1)
        self.assertEqual(result["same_document_groups"], [args["evidence_ids"]])
        self.assertTrue(result["possible_shared_origin"])
        self.assertNotIn("verified", result)
        item = self.store.get("evidence", "ev_injury")
        item["text"] = "Changed statement."
        self.store.put("evidence", item)
        with self.assertRaises(ToolError):
            self.local.call("compare_sources", args)

    def test_profile_exposes_only_user_preferences_and_additional_skills_keep_permissions(self):
        settings = {**self.store.settings(), "interests": ["战术", "青训"], "reading_language": "original", "fan_level": "beginner", "briefing_window_hours": 24}
        local = LocalTools(self.store, self.store.new_run(allow_network=False), settings, agent.load_skill)
        profile = local.call("reader_profile", {})
        self.assertEqual(profile["interests"], ["战术", "青训"])
        self.assertEqual(profile["briefing_window_hours"], 24)
        self.assertNotIn("api_key", json.dumps(profile))
        for skill in ("transfer_check", "tactics_explainer"):
            self.assertTrue(local.call("use_skill", {"skill": skill})["scope_unchanged"])
        self.assertFalse(local.allow_network)
        self.assertFalse(local.review)
        self.assertEqual(agent.route_skill({"skill": "auto", "question": "这条转会报道可信吗"}), "transfer_check")
        self.assertEqual(agent.route_skill({"skill": "auto", "question": "解释这篇战术文章"}), "tactics_explainer")

    def test_generic_search_uses_saved_interests_but_specific_player_queries_stay_specific(self):
        settings = {**self.store.settings(), "interests": ["转会"]}
        local = LocalTools(self.store, self.store.new_run(), settings, agent.load_skill)
        self.assertEqual(local.call("search_evidence", {"query": "球队资料"})["results"][0]["id"], "ev_transfer")
        self.assertEqual(local.call("search_evidence", {"query": "injury"})["results"][0]["id"], "ev_injury")


class ArticleQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_article_is_actually_read_before_first_model_request(self):
        store = FakeStore()
        store.new_run(input_evidence_ids=["ev_injury"], question="解释这篇文章")
        contexts = []
        async def answer(config, messages, **kwargs):
            contexts.extend(messages)
            return {"content": "来源称球员暂未参加训练。[ev_injury]"}
        with patch.object(agent, "request_completion", new=answer):
            await agent.run_agent(store, "run_one", asyncio.Event())
        result = store.get("runs", "run_one")
        self.assertEqual(result["status"], "completed")
        self.assertIn("ev_injury", result["read_evidence_ids"])
        self.assertIn("Medical update", json.dumps(contexts))

    async def test_selected_hidden_article_fails_before_any_model_or_network_request(self):
        store = FakeStore()
        store.new_run(input_evidence_ids=["ev_result"], question="解释这篇文章")
        with patch.object(agent, "request_completion", new=AsyncMock()) as request:
            await agent.run_agent(store, "run_one", asyncio.Event())
        self.assertEqual(store.get("runs", "run_one")["status"], "failed")
        request.assert_not_awaited()


class SavedReaderPreferencesTests(unittest.TestCase):
    def test_preferences_saved_through_http_reach_real_agent_tools_and_prompt(self):
        from fastapi.testclient import TestClient
        from server.main import create_app
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory) / "preferences.db")
            with TestClient(app) as client:
                response = client.put("/api/settings", json={"personalization": {
                    "interests": ["战术", "青训"], "reading_language": "original",
                    "fan_level": "beginner", "briefing_window_hours": 24}})
                self.assertEqual(response.status_code, 200)
                store = app.state.store
                run = {"id": "reader_preferences", "team_id": "arsenal", "mode": "live", "skill": "ask", "question": "解释这篇战术文章"}
                local = LocalTools(store, run, store.settings(), agent.load_skill)
                profile = local.call("reader_profile", {})
                self.assertEqual(profile["interests"], ["战术", "青训"])
                self.assertEqual(profile["reading_language"], "original")
                self.assertEqual(profile["fan_level"], "beginner")
                self.assertEqual(profile["briefing_window_hours"], 24)
                prompt = agent.system_prompt(run, store.settings(), local, agent.load_skill("ask"))
                self.assertIn('"interests": ["战术", "青训"]', prompt)
                self.assertIn('"briefing_window_hours": 24', prompt)


if __name__ == "__main__":
    unittest.main()
