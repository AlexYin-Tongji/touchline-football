import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from xml.sax.saxutils import escape

from server import ingestion, research
from server.seed import seed
from server.storage import Store


BBC_FEED = "https://feeds.bbci.co.uk/sport/football/rss.xml"
SKY_FEED = "https://www.skysports.com/rss/11670"


def feed(items):
    return ("<rss><channel>" + "".join(
        "<item><title>" + escape(title) + "</title><link>" + escape(url)
        + "</link><description>" + escape(description) + "</description><pubDate>"
        + escape(stamp) + "</pubDate></item>"
        for title, url, description, stamp in items
    ) + "</channel></rss>").encode()


class NewsSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "sources.db")
        self.addCleanup(self.store.close)
        seed(self.store)

    def test_reviewed_feeds_are_available_without_guessing_sky_team_ids(self):
        arsenal = research.source_catalog(self.store, "arsenal")
        self.assertIn(BBC_FEED, {item["url"] for item in arsenal})
        self.assertIn(SKY_FEED, {item["url"] for item in arsenal})
        for team_id in research.CATALOG:
            with self.subTest(team=team_id):
                sources = research.source_catalog(self.store, team_id)
                self.assertIn(BBC_FEED, {item["url"] for item in sources})
                sky = [item for item in sources if "skysports.com" in item["url"]]
                self.assertEqual([item["url"] for item in sky], [SKY_FEED] if team_id == "arsenal" else [])

    def test_explicit_publisher_name_is_not_replaced_by_bbc(self):
        source = {"id": "other_publisher", "name": "另一家媒体", "url": "https://www.skysports.com/rss/11670", "kind": "rss"}
        with patch.dict(research.CATALOG, {"arsenal": [source]}):
            selected = next(item for item in research.source_catalog(self.store, "arsenal") if item["id"] == source["id"])
        self.assertEqual(selected["name"], "另一家媒体")

    def test_new_defaults_preserve_custom_sources_and_disable_choices(self):
        for index in range(9):
            self.store.put("sources", {"id": f"custom_{index}", "name": f"Custom {index}",
                "url": f"https://example.com/feed/{index}", "kind": "rss", "enabled": True,
                "mode": "live", "team_id": "arsenal"})
        self.store.put("sources", {"id": "disable_bbc", "name": "BBC", "url": BBC_FEED,
            "kind": "rss", "enabled": False, "mode": "live", "team_id": "arsenal"})
        sources = research.source_catalog(self.store, "arsenal")
        self.assertTrue({f"custom_{index}" for index in range(9)} <= {item["id"] for item in sources})
        self.assertNotIn(BBC_FEED, {item["url"] for item in sources})
        self.store.put("sources", {"id": "enable_bbc", "name": "BBC", "url": BBC_FEED,
            "kind": "rss", "enabled": True, "mode": "live", "team_id": "liverpool"})
        self.assertEqual(sum(item["url"] == BBC_FEED for item in research.source_catalog(self.store, "liverpool")), 1)

    def test_sky_bst_dates_are_normalized_without_assuming_other_timezones(self):
        for url, stamp, expected in (
            (SKY_FEED, "Mon, 21 Sep 2026 15:20:00 BST", "2026-09-21T14:20:00+00:00"),
            (SKY_FEED, "Mon, 21 Dec 2026 15:20:00 GMT", "2026-12-21T15:20:00+00:00"),
            (SKY_FEED, "Mon, 21 Sep 2026 15:20:00", None),
            (BBC_FEED, "Mon, 21 Sep 2026 15:20:00 BST", None),
            ("https://example.com/feed", "Mon, 21 Sep 2026 15:20:00 BST", None),
            ("https://example.com/feed", "2026-09-21T15:20:00", None),
        ):
            with self.subTest(url=url, stamp=stamp):
                entries = research._feed_candidates(feed([("Arsenal update", "https://www.skysports.com/football/news/1/2", "", stamp)]), url)
                self.assertEqual(entries[0]["published_at"], expected)

    def test_publisher_media_and_other_squads_are_not_article_candidates(self):
        for url, title, description, expected in (
            ("https://www.bbc.com/sport/football/articles/arsenal", "Arsenal training update", "", True),
            ("https://www.bbc.com/sounds/play/arsenal", "Arsenal transfer latest", "", False),
            ("https://www.bbc.com/sport/av/football/arsenal", "Arsenal training update", "", False),
            ("https://www.bbc.com/sport/football/videos/arsenal", "Arsenal training update", "", False),
            ("https://www.bbc.com/sport/football/live/arsenal", "Arsenal latest", "", False),
            ("https://www.bbc.com/sport/football/articles/wsl", "Arsenal prepare for weekend", "The WSL side are training.", False),
            ("https://www.bbc.com/sport/football/articles/women", "Arsenal Women training update", "", False),
            ("https://www.bbc.com/sport/football/articles/youth", "Arsenal U-21 training update", "", False),
            ("https://www.bbc.com/sport/football/articles/academy", "Arsenal youth team prepare for weekend", "", False),
            ("https://www.skysports.com/watch/video/sports/football/arsenal", "Arsenal training update", "", False),
            ("https://www.skysports.com/football/live-blog/1/2", "Arsenal latest", "", False),
            ("https://www.skysports.com/football/news/11670/12345/arsenal", "Arsenal training update", "", True),
            ("https://www.bbc.com/sport/football/articles/a", "Arsenal train at academy ground", "Senior squad relocated for pitch maintenance.", True),
        ):
            with self.subTest(url=url, title=title):
                self.assertEqual(research._first_team_entry({"url": url, "title": title, "description": description}), expected)

    def test_first_team_youth_background_is_kept_but_youth_squads_are_filtered(self):
        for title, description, expected in (
            ("Arsenal youth product signs new first-team contract", "", True),
            ("Arsenal senior players return to training", "The men's first-team squad included several youth graduates.", True),
            ("Arsenal first-team contract announced", "The club's youth team graduate has signed a senior contract.", True),
            ("Arsenal youth team prepare for weekend", "", False),
            ("Arsenal prepare for European fixture", "The youth squad will play in the UEFA Youth League.", False),
            ("Arsenal U-21 prepare for weekend", "", False),
            ("Arsenal Women prepare for weekend", "", False),
            ("Arsenal prepare for weekend", "The WSL squad return to training.", False),
        ):
            with self.subTest(title=title, description=description):
                self.assertEqual(research._first_team_entry({
                    "url": "https://www.bbc.com/sport/football/articles/example",
                    "title": title, "description": description,
                }), expected)

    async def test_shared_global_feed_is_filtered_for_each_team_after_caching(self):
        sources = research.source_catalog(self.store, "arsenal")
        self.assertTrue(any(item["url"] == BBC_FEED for item in sources), "BBC global feed is missing")
        source = next(item for item in sources if item["url"] == BBC_FEED)
        body = feed([
            ("Arsenal training update", "https://www.bbc.com/sport/football/articles/arsenal", "First-team session", "Mon, 21 Sep 2026 15:20:00 GMT"),
            ("Liverpool training update", "https://www.bbc.com/sport/football/articles/liverpool", "First-team session", "Mon, 21 Sep 2026 15:20:00 GMT"),
            ("Chelsea training update", "https://www.bbc.com/sport/football/articles/chelsea", "First-team session", "Mon, 21 Sep 2026 15:20:00 GMT"),
            ("Arsenal Women training", "https://www.bbc.com/sport/football/articles/arsenal-women", "WSL team", "Mon, 21 Sep 2026 15:20:00 GMT"),
        ])
        empty = ([], [], {"id": "external", "name": "External", "state": "unavailable", "checked_at": ingestion.now()})
        with patch("server.research.source_catalog", side_effect=lambda store, team_id: [{**source, "team_id": team_id}]), \
                patch("server.research._fetch", new=AsyncMock(return_value=(body, BBC_FEED, "application/rss+xml"))) as fetch_index, \
                patch("server.research._web_news_index", new=AsyncMock(return_value=empty)), \
                patch("server.research._espn_news_index", new=AsyncMock(return_value=empty)):
            arsenal = await research.discover_news(self.store, "arsenal", "最近消息", 10)
            liverpool = await research.discover_news(self.store, "liverpool", "最近消息", 10)
        self.assertEqual([item["title"] for item in arsenal["candidates"]], ["Arsenal training update"])
        self.assertEqual([item["title"] for item in liverpool["candidates"]], ["Liverpool training update"])
        self.assertEqual(fetch_index.call_count, 1)
        cached = self.store.get_meta(research._index_key("arsenal", source))
        self.assertEqual(len(cached["entries"]), 4)
        self.assertTrue(all("team_id" not in item and "text" not in item for item in cached["entries"]))

    def test_custom_shared_flag_cannot_cross_team_cache_boundaries(self):
        source = {"id": "custom", "url": "https://example.com/feed", "builtin": False, "shared_index": True}
        self.assertNotEqual(research._index_key("arsenal", source), research._index_key("liverpool", source))

    def test_global_feed_requires_unambiguous_club_identity(self):
        for team_id, title, expected in (
            ("inter", "Inter Milan prepare for weekend fixture", True),
            ("inter", "Internazionale confirm training plans", True),
            ("inter", "Inter Miami prepare for weekend fixture", False),
            ("ac-milan", "AC Milan confirm training plans", True),
            ("ac-milan", "Inter Milan confirm training plans", False),
            ("ac-milan", "国际米兰确认训练安排", False),
            ("nice", "Arsenal make a nice gesture to supporters", False),
            ("nice", "OGC Nice prepare for weekend fixture", True),
            ("lens", "Liverpool's training seen through the lens of a camera", False),
            ("lens", "RC Lens prepare for weekend fixture", True),
            ("angers", "Chelsea transfer decision angers supporters", False),
            ("angers", "Angers SCO prepare for weekend fixture", True),
            ("angers", "SCO Angers prepare for weekend fixture", True),
        ):
            with self.subTest(team=team_id, title=title):
                self.assertEqual(research.global_feed_mentions_team(team_id, {"title": title}), expected)

    def test_bbc_feed_redirects_stay_in_the_reviewed_publisher_hosts(self):
        source = {"id": "bbc_football", "url": BBC_FEED, "builtin": True, "kind": "rss"}
        hosts = research._source_hosts(source)
        self.assertEqual(research._checked_url("https://www.bbc.com/sport/football/articles/example", hosts), "https://www.bbc.com/sport/football/articles/example")
        for url in ("https://www.skysports.com/football/news/1", "https://fake.bbc.com/article", "http://127.0.0.1/private"):
            with self.subTest(url=url), self.assertRaises(ingestion.IngestionError):
                research._checked_url(url, hosts)


if __name__ == "__main__":
    unittest.main()
