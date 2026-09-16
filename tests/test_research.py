import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server import ingestion, research
from server.seed import seed
from server.storage import Store


SOURCE = {"id": "custom_source", "name": "Configured source", "url": "https://example.com/news", "kind": "html", "team_id": "arsenal", "mode": "live", "enabled": True, "builtin": False}
ARTICLE = b'<html><title>Saka injury update</title><meta property="article:published_time" content="2026-09-08T12:00:00Z"><article><p>Saka has an injury and is being assessed ahead of the match against Napoli. The club has not announced the final squad.</p></article></html>'


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        espn = patch("server.research._espn_news_index", new=AsyncMock(return_value=([], [], {"id": "espn_news", "name": "ESPN 新闻", "state": "unavailable", "checked_at": ingestion.now()})))
        espn.start()
        self.addCleanup(espn.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "research.db")
        seed(self.store)
        self.store.put("sources", SOURCE)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def candidate(self, **patch_values):
        item = {"id": "candidate_test", "mode": "live", "team_id": "arsenal", "url": "https://example.com/news/saka", "title": "Saka injury update", "description": "Saka is being assessed ahead of Napoli.", "published_at": "2026-09-08T12:00:00Z", "source_id": SOURCE["id"], "source_name": SOURCE["name"], **patch_values}
        self.store.put("candidates", item)
        return item

    def test_sitemap_modified_time_is_not_publication_time(self):
        entries = research._sitemap_candidates(b'<urlset><url><loc>https://www.arsenal.com/news/saka-injury-update-aABCD12345678</loc><lastmod>2026-09-08T12:00:00Z</lastmod></url><url><loc>https://www.arsenal.com/gallery/photos-aABCD12345678</loc></url></urlset>')
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "saka injury update")
        self.assertIsNone(entries[0]["published_at"])
        self.assertTrue(entries[0]["source_modified_at"])

    def test_disabled_configured_url_overrides_builtin_source(self):
        builtin = research.CATALOG["arsenal"][0]
        self.store.put("sources", {**SOURCE, "id": "user_disabled", "url": builtin["url"], "enabled": False})
        self.assertNotIn(builtin["id"], [source["id"] for source in research.source_catalog(self.store, "arsenal")])

    def test_feed_discovery_keeps_short_descriptions_without_importing_them(self):
        entries = research._feed_candidates(b'<rss><channel><item><title>Saka update</title><link>https://example.com/news/saka</link><description>Fitness.</description></item></channel></rss>', SOURCE["url"])
        self.assertEqual(len(entries), 1)
        self.assertNotIn("text", entries[0])

    def test_embedded_article_content_excludes_promoted_news(self):
        payload = {"props": {"pageProps": {"article": {"title": "Arsenal training", "publicationDate": "2026-09-08T12:00:00Z", "articleBody": [{"type": "TEXT", "innerText": "The players completed a full training session. The coach will assess fitness tomorrow."}, {"type": "PROMOTEDARTICLE", "title": "UNRELATED SCORES 7-0"}]}}}}
        body = ('<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + '</script><main>Loading</main></html>').encode()
        entry = ingestion.extract_html(body, "https://www.arsenal.com/news/training", "Official")[0]
        self.assertNotIn("UNRELATED", entry["text"])
        self.assertNotIn("Loading", entry["text"])
        self.assertEqual(entry["published_at"], "2026-09-08T12:00:00+00:00")

    def test_safe_view_preserves_source_and_original_but_removes_whole_result_paragraph(self):
        safe = "Arsenal's flight to Naples has been delayed by air traffic restrictions. The club is discussing travel arrangements with airport staff and will confirm when the squad can depart."
        history = "The team won 2-1 on Sunday, with Saka scoring the late goal and a defender receiving a red card."
        item = {"id": "ev_original", "team_id": "arsenal", "mode": "live", "origin": "web", "spoiler": True,
                "title": "Air traffic chaos delays Arsenal flight to Naples for Champions League",
                "url": "https://www.theguardian.com/football/2026/sep/08/air-traffic-chaos-delays-arsenal-flight-to-naples-for-champions-league",
                "published_at": "2026-09-08T18:38:24Z", "text": safe + "\n" + history}
        view = ingestion.safe_evidence_view(item)
        self.assertIsNotNone(view)
        self.assertTrue(view["spoiler_redacted"])
        self.assertFalse(view["spoiler"])
        self.assertEqual(view["id"], item["id"])
        self.assertEqual(view["url"], item["url"])
        self.assertEqual(view["text"], safe)
        self.assertNotIn("2-1", view["text"])
        self.assertIn(history, item["text"])
        self.assertTrue(item["spoiler"])

    def test_safe_view_keeps_manual_spoilers_and_result_headlines_hidden(self):
        item = {"origin": "web", "spoiler": True, "title": "Travel arrangements", "text": "The club's travel arrangements will be confirmed after the meeting with airport staff. " * 4 + "\nThe team won 2-1."}
        self.assertIsNone(ingestion.safe_evidence_view({**item, "origin": "manual"}))
        self.assertIsNone(ingestion.safe_evidence_view({**item, "title": "Arsenal win 2-1 in Naples"}))
        self.assertIsNone(ingestion.safe_evidence_view({**item, "title": "阿森纳晋级决赛"}))
        self.assertIsNone(ingestion.safe_evidence_view({**item, "url": "https://example.com/news/arsenal-win-two-one"}))
        self.assertIsNone(ingestion.safe_evidence_view({**item, "text": "Won\n2\n-\n1\nThe\nclub\ntravel\nplans"}))
        self.assertIsNone(ingestion.safe_evidence_view({**item, "text": "The team won 2-1.\nTravel later."}))
        self.assertFalse(ingestion.spoiler_title("Champions League travel on 2026-09-09"))

    def test_automatic_spoiler_flag_uses_the_same_result_vocabulary_as_safe_view(self):
        for title in ("Bench Cam: Mikel's Chelsea comeback", "阿森纳晋级决赛", "Arsenal's Premier League title", "Saka scores a late goal"):
            self.assertTrue(ingestion.potential_spoiler(title, "Official club news."))
            self.assertTrue(ingestion.spoiler_title(title))
        self.assertFalse(ingestion.potential_spoiler("Champions League travel", "The squad will travel on 2026-09-09."))

    def test_chinese_entity_topic_query_rejects_unrelated_and_wrong_player(self):
        self.assertGreater(research.relevance("arsenal", "继续跟进萨卡伤情", {"title": "Saka injury fitness update"}), 0)
        self.assertEqual(research.relevance("arsenal", "继续跟进萨卡伤情", {"title": "Rice injury fitness update"}), 0)
        self.assertEqual(research.relevance("arsenal", "继续跟进萨卡伤情", {"title": "Saka scores a great goal"}), 0)
        self.assertGreater(research.relevance("arsenal", "阿森纳最近有什么变化", {"title": "Arteta press conference"}), 0)

    def test_preview_query_finds_real_official_headline_idioms(self):
        for title in ("arteta on champions league return and napoli trip", "in form calafiori relishing napoli test"):
            self.assertGreater(research.relevance("arsenal", "Arsenal最近赛前消息", {"title": title}), 0)
            self.assertEqual(research.relevance("arsenal", "萨卡赛前消息", {"title": title}), 0)
        self.assertEqual(research.relevance("arsenal", "Arsenal赛前消息", {"title": "Medical test completed by staff"}), 0)

    async def test_discover_persists_scoped_candidates_and_keeps_partial_failures(self):
        html = b'<main><a href="/news/saka">Saka injury update</a><a href="http://127.0.0.1/private">Ignore private</a><a href="https://other.example/news/injury">Wrong publisher injury</a></main>'
        async def fetch(url, source):
            if source["id"] != SOURCE["id"]:
                raise ingestion.IngestionError("Publisher is unavailable")
            return html, url, "text/html"
        with patch("server.research._fetch", side_effect=fetch):
            result = await research.discover_news(self.store, "arsenal", "萨卡伤情", 6)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertTrue(result["errors"])
        self.assertIn("标题和摘要", result["notice"])
        self.assertIn("不代表", result["notice"])
        candidate = result["candidates"][0]
        self.assertEqual(self.store.get("candidates", candidate["id"])["team_id"], "arsenal")
        self.assertEqual(self.store.list("evidence", "live"), [])

    async def test_scope_and_match_validation_precede_network_access(self):
        self.candidate()
        with patch("server.research._fetch", new_callable=AsyncMock) as fetch:
            for team, cid, mid in (("liverpool", "candidate_test", None), ("arsenal", "unknown", None), ("arsenal", "candidate_test", "match_demo_arsenal_next")):
                with self.assertRaises(ingestion.IngestionError):
                    await research.read_candidate(self.store, team, cid, match_id=mid)
            fetch.assert_not_called()

    async def test_collect_returns_duplicate_ids_for_baseline(self):
        candidate = self.candidate()
        with patch("server.research.discover_news", new=AsyncMock(return_value={"candidates": [candidate], "errors": []})), patch("server.research._fetch", new=AsyncMock(return_value=(ARTICLE, candidate["url"], "text/html"))):
            first = await research.collect_team(self.store, "arsenal", "萨卡伤情", 1)
            second = await research.collect_team(self.store, "arsenal", "萨卡伤情", 1)
        self.assertEqual(first["added"], 1)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(first["evidence_ids"], second["evidence_ids"])
        self.assertEqual(len(self.store.list("events", "live")), 1)

    async def test_match_association_requires_opponent_and_time_window(self):
        candidate = self.candidate()
        match = {"id": "match_live", "team_id": "arsenal", "mode": "live", "opponent": "那不勒斯", "kickoff": "2026-09-09T19:00:00Z", "evidence_ids": []}
        self.store.put("matches", match)
        self.assertTrue(research.candidate_matches(self.store, "arsenal", candidate, match["id"]))
        with patch("server.research._fetch", new=AsyncMock(return_value=(ARTICLE, candidate["url"], "text/html"))):
            imported = await research.read_candidate(self.store, "arsenal", candidate["id"], match_id=match["id"])
        self.assertIn(imported["evidence"]["id"], self.store.get("matches", match["id"])["evidence_ids"])
        match["opponent"] = "Liverpool"
        self.store.put("matches", match)
        with patch("server.research._fetch", new=AsyncMock(return_value=(ARTICLE, candidate["url"], "text/html"))):
            with self.assertRaisesRegex(ingestion.IngestionError, "对手"):
                await research.read_candidate(self.store, "arsenal", candidate["id"], match_id=match["id"])
        self.assertEqual(self.store.get("matches", match["id"])["evidence_ids"], [])
        match["opponent"] = "Napoli"
        match["kickoff"] = "2025-01-01T19:00:00Z"
        self.store.put("matches", match)
        self.assertFalse(research.candidate_matches(self.store, "arsenal", candidate, match["id"]))

    async def test_cancel_does_not_import_or_swallow_cancellation(self):
        self.candidate()
        started = asyncio.Event()
        async def stalled(*args):
            started.set()
            await asyncio.Event().wait()
        with patch("server.research._fetch", side_effect=stalled):
            task = asyncio.create_task(research.read_candidate(self.store, "arsenal", "candidate_test"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.store.list("evidence", "live"), [])

    async def test_custom_source_fetch_retains_domain_guard(self):
        with patch("server.ingestion.fetch_document", return_value=(ARTICLE, "https://example.com/news/a", "text/html")) as fetch:
            await research._fetch("https://example.com/news/a", SOURCE)
            self.assertEqual(fetch.call_args.kwargs["allowed_hosts"], {"example.com"})
        with self.assertRaises(ingestion.IngestionError):
            research._checked_url("https://other.example/news/a", {"example.com"})
        with self.assertRaises(ingestion.IngestionError):
            research._checked_url("http://127.0.0.1/news", {"127.0.0.1"})


if __name__ == "__main__":
    unittest.main()
