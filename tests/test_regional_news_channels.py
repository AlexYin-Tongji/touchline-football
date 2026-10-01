import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from server import ingestion, news_sources, research
from server.personal_app import source_tier
from tests.support import IsolatedAppCase


class RegionalNewsChannelTests(IsolatedAppCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.store = self.app.state.store
        self.recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    def channel(self, identity, team="bologna"):
        return next(source for source in research.source_catalog(self.store, team) if source["id"] == identity)

    def feed(self, host, team="Bologna"):
        return ('<rss><channel><item><title>' + team + ': public first team update</title><link>https://'
                + host + '/news/first-team-update</link><pubDate>' + self.recent
                + '</pubDate><category>Football</category></item></channel></rss>').encode()

    def test_declared_regional_channels_are_identical_across_the_complete_club_catalog(self):
        expected = {"tmw_football", "tmw_serie_a", "footmercato_football"}
        baseline = None
        for team in research.CATALOG:
            channels = {source["id"]: source for source in news_sources.additional_sources(team) if source["id"] in expected}
            self.assertEqual(set(channels), expected)
            locations = {identity: row["url"] for identity, row in channels.items()}
            baseline = baseline or locations
            self.assertEqual(locations, baseline)
            for source in channels.values():
                self.assertTrue(source["url"].startswith("https://"))
                self.assertTrue(source["declaration_url"].startswith("https://"))
                self.assertTrue(source["global_feed"] and source["shared_index"])
        self.assertEqual(self.channel("tmw_football")["publisher_id"], self.channel("tmw_serie_a")["publisher_id"])

    def test_regional_feed_identity_and_squad_scope_stay_explicit(self):
        for team, title, scope, expected in (("bologna", "Bologna prepare for the next match", "Serie A", True),
                                              ("bologna", "Fiorentina prepare for the next match", "Serie A", False),
                                              ("bologna", "Bologna prepare for the next match", "Primavera", False),
                                              ("angers", "SCO Angers prépare le prochain match", "Football", True),
                                              ("angers", "SCO Angers prépare le prochain match", "Féminines", False)):
            entry = {"url": "https://www.footmercato.net/a123-news", "title": title, "source_scope": scope}
            self.assertEqual(research.global_feed_mentions_team(team, entry) and research._first_team_entry(entry), expected)

    async def test_one_regional_subscription_is_cached_once_for_multiple_clubs(self):
        bologna = self.channel("tmw_serie_a")
        arsenal = self.channel("tmw_serie_a", "arsenal")
        with patch.object(research, "_fetch", new=AsyncMock(return_value=(self.feed("www.tuttomercatoweb.com"), bologna["url"], "application/rss+xml"))) as fetch:
            first = await research._source_index(self.store, "bologna", bologna)
            second = await research._source_index(self.store, "arsenal", arsenal)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(first[2]["state"], "fresh")
        self.assertEqual(second[2]["state"], "cached")

    async def test_duplicate_channels_do_not_hide_missing_results_from_supplementary_search(self):
        sources = [self.channel("tmw_football"), self.channel("tmw_serie_a")]
        canonical = "https://www.tuttomercatoweb.com/serie-a/bologna-update-123"
        supplementary = {"url": "https://www.bbc.com/sport/football/articles/bologna-update",
                         "title": "Bologna first team update from another publisher", "published_at": self.recent,
                         "source_id": "search_bbc", "source_name": "BBC", "publisher_id": "bbc"}
        async def index(store, team_id, source, **kwargs):
            entry = {"url": canonical + "?utm_source=" + source["id"],
                     "title": "Bologna first team update", "published_at": self.recent}
            return [entry], [], {"state": "fresh", "checked_at": ingestion.now()}
        web = AsyncMock(return_value=([supplementary], [], {"id": "bing_news", "state": "fresh", "checked_at": ingestion.now()}))
        empty = ([], [], {"id": "espn_news", "state": "fresh", "checked_at": ingestion.now()})
        with patch.object(research, "source_catalog", return_value=sources), \
                patch.object(research, "_source_index", side_effect=index), \
                patch.object(research, "_web_news_index", web), \
                patch.object(research, "_espn_news_index", new=AsyncMock(return_value=empty)):
            found = await research.discover_news(self.store, "bologna", limit=2, max_age_days=7)
        self.assertEqual(web.await_count, 1)
        self.assertEqual({entry["url"] for entry in found["candidates"]}, {canonical, supplementary["url"]})
        self.assertEqual(found["matched_count"], 2)
        self.assertEqual(len(found["candidates"]), 2)

    async def test_regional_metadata_and_blocked_body_cannot_create_news_evidence(self):
        source = self.channel("tmw_serie_a")
        async def fetch(url, descriptor):
            body = self.feed("www.tuttomercatoweb.com") if url == source["url"] else b'<html><title>Bologna first team update</title><body>Cookie and site navigation labels without an owned headline or story body.</body></html>'
            return body, url, "text/html"
        empty = ([], [], {"id": "empty", "state": "fresh", "checked_at": ingestion.now()})
        with patch.object(research, "source_catalog", return_value=[source]), patch.object(research, "_fetch", side_effect=fetch), \
                patch.object(research, "_web_news_index", new=AsyncMock(return_value=empty)), patch.object(research, "_espn_news_index", new=AsyncMock(return_value=empty)):
            found = await research.collect_team(self.store, "bologna", limit=1, max_age_days=7)
        self.assertEqual(found["evidence_ids"], [])
        self.assertEqual(found["sources"][0]["read_attempted"], 1)
        self.assertEqual(found["sources"][0]["readable_count"], 0)
        self.assertEqual(found["source_errors"][0]["code"], "content_unavailable")

    def test_local_language_article_container_excludes_sidebar_and_registry_cannot_grant_access(self):
        body = ('<html><h1>First team update</h1><div class="articolo"><div class="text"><p>'
                + "The football club published this limited first team update with a clear public source. " * 9
                + '</p></div><aside><p>UNRELATED_NEWS_SENTINEL</p></aside></div></html>').encode()
        article = ingestion.extract_html(body, "https://www.tuttomercatoweb.com/serie-a/public-update", "TMW")[0]
        self.assertEqual(article["content_method"], "html_article")
        self.assertTrue(article["_article_owned"])
        self.assertNotIn("UNRELATED_NEWS_SENTINEL", article["text"])
        for host in ("www.tuttomercatoweb.com", "www.footmercato.net"):
            self.assertEqual(source_tier("https://" + host + "/news/update")["tier"], "press")
            self.assertFalse(source_tier("https://" + host + ".unreviewed.example/news/update")["tier_known"])
        source = self.channel("tmw_serie_a")
        with self.assertRaises(ingestion.IngestionError):
            research._checked_url("https://www.footmercato.net/a123", research._source_hosts(source))
