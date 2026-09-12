"""Retrieval quality, freshness, local reuse and source boundaries."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server import ingestion, research
from server.agent_tools import LocalTools, ToolError
from server.seed import seed
from server.storage import Store
from tests.test_agent import FakeStore
from tests.test_research import SOURCE, ARTICLE


class DiscoveryEnhancementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "collection.db")
        seed(self.store)
        self.store.put("sources", SOURCE)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def feed(self, *items):
        return ("<rss><channel>" + "".join(
            f"<item><title>{title}</title><link>https://example.com/news/{slug}</link>"
            f"<pubDate>{date}</pubDate></item>" for title, slug, date in items
        ) + "</channel></rss>").encode()

    def source(self):
        return {**SOURCE, "kind": "rss"}

    async def test_index_reused_across_different_questions_without_new_network_calls(self):
        source = self.source()
        feed = self.feed(("Saka injury assessment", "saka", ingestion.now()),
                         ("Rice signs a new contract", "rice", ingestion.now()))
        with patch("server.research.source_catalog", return_value=[source]), patch("server.research._fetch", new=AsyncMock(return_value=(feed, source["url"], "application/rss+xml"))) as fetch:
            first = await research.discover_news(self.store, "arsenal", "萨卡伤情")
            second = await research.discover_news(self.store, "arsenal", "赖斯续约")
        self.assertEqual(fetch.await_count, 1)
        self.assertEqual(first["candidates"][0]["title"], "Saka injury assessment")
        self.assertEqual(second["candidates"][0]["title"], "Rice signs a new contract")
        self.assertEqual(second["sources"][0]["state"], "cached")
        self.assertEqual(self.store.list("evidence", "live"), [])

    async def test_failed_source_returns_timestamped_old_index_and_expired_cache_is_ignored(self):
        source = self.source()
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        entry = {"title": "Saka injury assessment", "url": "https://example.com/news/saka", "published_at": old}
        key = research._index_key("arsenal", source)
        self.store.set_meta(key, {"entries": [entry], "fetched_at": old})
        with patch("server.research.source_catalog", return_value=[source]), patch("server.research._fetch", new=AsyncMock(side_effect=ingestion.IngestionError("暂不可用"))):
            result = await research.discover_news(self.store, "arsenal", "萨卡伤情")
            self.assertEqual(result["sources"][0]["state"], "stale")
            self.assertEqual(result["candidates"][0]["index_checked_at"], old)
            self.assertTrue(result["errors"])
            self.store.set_meta(key, {"entries": [entry], "fetched_at": "2020-01-01T00:00:00Z"})
            expired = await research.discover_news(self.store, "arsenal", "萨卡伤情")
            self.assertEqual(expired["candidates"], [])

    async def test_category_date_and_first_team_filters_apply_before_candidate_budget(self):
        source = self.source()
        recent = ingestion.now()
        feed = self.feed(("Saka injury assessment", "injury", recent),
                         ("Rice new contract signing", "contract", recent),
                         ("Arsenal Women injury update", "women-injury", recent),
                         ("Arsenal under-21 injury update", "u21-injury", recent),
                         ("Arsenal injury archive", "old-injury", "2020-01-01T00:00:00Z"),
                         ("Arsenal injury unknown date", "unknown-injury", ""))
        with patch("server.research.source_catalog", return_value=[source]), patch("server.research._fetch", new=AsyncMock(return_value=(feed, source["url"], "application/rss+xml"))):
            result = await research.discover_news(self.store, "arsenal", "", category="injury", max_age_days=7)
        self.assertEqual([item["title"] for item in result["candidates"]], ["Saka injury assessment"])
        self.assertEqual(result["candidates"][0]["categories"], ["injury"])

    async def test_tracking_urls_deduplicate_but_article_identifiers_stay_distinct(self):
        source = self.source()
        feed = self.feed(("Saka injury assessment", "saka?utm_source=newsletter", ingestion.now()),
                         ("Saka injury assessment", "saka?utm_source=social", ingestion.now()),
                         ("Saka injury assessment", "saka?id=another-article", ingestion.now()))
        with patch("server.research.source_catalog", return_value=[source]), patch("server.research._fetch", new=AsyncMock(return_value=(feed, source["url"], "application/rss+xml"))):
            result = await research.discover_news(self.store, "arsenal", "萨卡伤情")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertTrue(any("id=another-article" in item["url"] for item in result["candidates"]))

    async def test_fresh_articles_are_reused_with_original_collection_time_and_source_checks(self):
        candidate = {"id": "candidate_cache", "mode": "live", "team_id": "arsenal", "source_id": SOURCE["id"],
                     "url": "https://example.com/news/saka", "title": "Saka injury update"}
        self.store.put("candidates", candidate)
        with patch("server.research._fetch", new=AsyncMock(return_value=(ARTICLE, candidate["url"], "text/html"))) as fetch:
            first = await research.read_candidate(self.store, "arsenal", candidate["id"])
            second = await research.read_candidate(self.store, "arsenal", candidate["id"])
            self.assertEqual(fetch.await_count, 1)
            self.assertTrue(second["cached"])
            self.assertTrue(second["duplicate"])
            self.assertEqual(first["evidence"]["fetched_at"], second["evidence"]["fetched_at"])
            self.store.put("sources", {**SOURCE, "url": "https://different.example/news"})
            with self.assertRaisesRegex(ingestion.IngestionError, "域名"):
                await research.read_candidate(self.store, "arsenal", candidate["id"])
            self.store.put("sources", {**SOURCE, "enabled": False})
            with self.assertRaisesRegex(ingestion.IngestionError, "停用"):
                await research.read_candidate(self.store, "arsenal", candidate["id"])
            self.assertEqual(fetch.await_count, 1)

    def test_chinese_opponent_and_tactics_queries_remain_specific(self):
        self.assertGreater(research.relevance("liverpool", "马竞赛前消息", {"title": "Atletico Madrid team news"}), 0)
        self.assertEqual(research.relevance("liverpool", "马竞赛前消息", {"title": "Chelsea team news"}), 0)
        self.assertGreater(research.relevance("arsenal", "阿森纳逼抢战术", {"title": "Arsenal pressing tactics explained"}), 0)
        self.assertEqual(research.relevance("arsenal", "阿森纳逼抢战术", {"title": "Arsenal ticket details"}), 0)

    def test_modification_time_cannot_make_an_undated_article_recent(self):
        self.assertFalse(research.within_days({"published_at": None, "source_modified_at": ingestion.now()}, 7))
        self.assertFalse(research.within_days({"published_at": "2099-01-01T00:00:00Z"}, 7))


class LocalRetrievalEnhancementTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.local = LocalTools(self.store, self.store.new_run(), self.store.settings())

    def test_player_name_in_chinese_cannot_return_different_players_injury(self):
        for eid, name in (("saka", "Saka"), ("rice", "Rice")):
            self.store.put("evidence", {"id": eid, "mode": "live", "team_id": "arsenal", "title": name + " injury news",
                                        "text": name + " is being assessed.", "published_at": ingestion.now(), "spoiler": False})
        result = self.local.call("search_evidence", {"query": "萨卡最近伤情", "category": "injury", "max_age_days": 7})
        self.assertEqual([row["id"] for row in result["results"]], ["saka"])
        self.assertEqual(self.local.read_ids, set())

    def test_source_overview_needs_no_network_and_is_in_chinese(self):
        result = self.local.call("source_overview", {})
        self.assertEqual(result["sources"][0]["name"], "阿森纳官网")
        self.assertEqual(result["sources"][0]["source_type"], "official")
        self.assertEqual(self.local.network_queries, 0)

    def test_filters_reject_unknown_categories_and_non_integer_days(self):
        for values in ({"category": "whatever"}, {"max_age_days": True}, {"max_age_days": 0}):
            with self.assertRaises(ToolError):
                self.local.call("search_evidence", {"query": "injury", **values})


if __name__ == "__main__":
    unittest.main()
