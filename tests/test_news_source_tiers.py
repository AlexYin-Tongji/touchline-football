"""Reviewed news attribution does not grant access or rewrite provider facts."""
import copy
import unittest

from server import news_source_tiers, provenance, providers
from server.club_library import CLUBS
from server.personal_app import source_tier
from server.provenance import SourceTier


class ReviewedNewsTierTests(unittest.TestCase):
    def setUp(self):
        self.registry = provenance.snapshot_registry()
        self.addCleanup(provenance.restore_registry, self.registry)

    def test_every_reviewed_club_official_source_has_an_official_badge(self):
        self.assertEqual(len(news_source_tiers.OFFICIAL_SOURCES), len(CLUBS))
        for _, host, _ in news_source_tiers.OFFICIAL_SOURCES:
            with self.subTest(host=host):
                label = source_tier(f"https://www.{host}/news/men/1")
                self.assertEqual(label["tier"], "official")
                self.assertEqual(label["tier_label"], "官方")

    def test_reviewed_media_feeds_and_articles_have_media_badges(self):
        for url in ("https://feeds.bbci.co.uk/sport/football/rss.xml", "https://www.bbc.com/sport/football/articles/123",
                    "https://www.theguardian.com/football/2026/oct/01/story", "https://www.skysports.com/football/news/123",
                    "https://rmcsport.bfmtv.com/football/story_av-123.html", "https://as.com/futbol/story/",
                    "https://www.tycsports.com/futbol-internacional.html", "https://www.marca.com/futbol/story.html",
                    "https://www.gazzetta.it/Calcio/story.shtml", "https://newsfeed.kicker.de/news/aktuell"):
            with self.subTest(url=url):
                label = source_tier(url)
                self.assertEqual(label["tier"], "press")
                self.assertEqual(label["tier_label"], "媒体")

    def test_transport_permission_and_lookalike_hosts_do_not_grant_a_tier(self):
        for url in ("https://www.bing.com/search?q=football", "https://unknown-football-blog.example/story",
                    "https://arsenal.com.unreviewed.example/news", "https://unreviewed.arsenal.com/news",
                    "https://bbc.co.uk.unreviewed.example/sport", "ftp://www.arsenal.com/news"):
            with self.subTest(url=url):
                self.assertFalse(source_tier(url)["tier_known"])

    def test_a_catalog_source_label_cannot_make_another_host_official(self):
        club = copy.deepcopy(CLUBS[0])
        club["research_sources"] = [{"id": "official_wrong_host", "url": "https://unreviewed.example/news"}]
        self.assertEqual(list(news_source_tiers._official_sources([club])), [])

    def test_provider_and_community_registrations_are_preserved(self):
        providers.load()
        before = provenance.tier_for_url("https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1")
        source_tier("https://www.fcbarcelona.com/en/football/first-team/news")
        self.assertEqual(before, SourceTier.DATASET)
        self.assertEqual(provenance.tier_for_url("https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1"), before)
        self.assertEqual(source_tier("https://www.espn.com/soccer/story/_/id/123/football")["tier"], "dataset")
        self.assertEqual(source_tier("https://www.reddit.com/r/soccer/comments/abc/")["tier"], "community")


if __name__ == "__main__":
    unittest.main()
