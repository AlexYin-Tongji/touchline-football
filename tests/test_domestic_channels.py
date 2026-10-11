"""Domestic channels: declared only where a probe found entries, and scoped.

The failure this file guards against is a source that occupies a slot and can
never yield anything — which is why `espn_soccer` and `gazzetta_football` were
deleted from the reviewed list rather than left in place. Every channel here
must therefore carry the declaration evidence a reviewer needs.
"""
import unittest

from server import news_sources, research
from server.personal_app import source_tier


class DomesticChannelTests(unittest.TestCase):
    def channels(self):
        return news_sources._domestic_channels()

    def test_every_declared_channel_carries_its_probe_evidence(self):
        for channel in self.channels():
            with self.subTest(channel=channel.get("id")):
                self.assertTrue(channel.get("id"))
                self.assertIn(channel["kind"], {"rss", "html", "json"})
                self.assertTrue(channel["url"].startswith("https://"))
                self.assertEqual(channel["declaration_url"], channel["url"])
                self.assertRegex(channel["reviewed_at"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertTrue(channel.get("allowed_hosts"))
                self.assertTrue(channel.get("description"))

    def test_the_declared_hosts_all_grant_network_reach(self):
        # A built-in source whose host is not in the reviewed allowlist is
        # refused outright by `_source_hosts`, so a missed host makes the
        # channel look permanently broken rather than misconfigured.
        for host in news_sources.domestic_hosts():
            with self.subTest(host=host):
                self.assertIn(host, research.TRUSTED_HOSTS)

    def test_channels_are_shared_identically_by_every_club(self):
        first = news_sources.domestic_sources("arsenal")
        for team_id in ("real-madrid", "bologna", "barcelona"):
            with self.subTest(team=team_id):
                self.assertEqual([source["id"] for source in news_sources.domestic_sources(team_id)],
                                 [source["id"] for source in first])
        self.assertTrue(first)

    def test_an_unknown_team_gets_no_channels(self):
        self.assertEqual(news_sources.domestic_sources("not-a-club"), [])

    def test_a_channel_resolves_entirely_inside_its_declared_hosts(self):
        # Article pages on a Chinese portal commonly sit on a different host
        # from the section index, so the declaration has to name both or the
        # reader drops every candidate at the host check.
        for source in news_sources.domestic_sources("arsenal"):
            with self.subTest(source=source["id"]):
                hosts = research._source_hosts(source)
                self.assertEqual(hosts, set(source["allowed_hosts"]))

    def test_declared_hosts_are_registered_press_sources(self):
        for host in ("sports.163.com", "www.163.com"):
            with self.subTest(host=host):
                tier = source_tier(f"https://{host}/news/update")
                self.assertEqual(tier["tier"], "press")
                self.assertTrue(tier["tier_known"])
                self.assertFalse(source_tier(f"https://{host}.unreviewed.example/news/update")["tier_known"])


class ChineseClubScopeTests(unittest.TestCase):
    """Chinese aliases have no word boundary, so a bare place name misfires."""

    def mentions(self, title, team_id):
        return news_sources.global_feed_mentions_team(team_id, {"title": title, "url": "https://sports.163.com/gjb/x.html"})

    def test_the_full_club_name_still_identifies_each_milan_side(self):
        title = "国际米兰 vs AC米兰 焦点战"
        self.assertTrue(self.mentions(title, "inter"))
        self.assertTrue(self.mentions(title, "ac-milan"))

    def test_one_milan_side_does_not_claim_the_other_side_story(self):
        self.assertFalse(self.mentions("AC米兰签下新前锋", "inter"))
        self.assertFalse(self.mentions("国际米兰宣布新任主帅", "ac-milan"))

    def test_a_city_reference_alone_identifies_neither_side(self):
        # "米兰" is Milan the city, and it is a substring of both club names.
        self.assertFalse(self.mentions("米兰城德比前瞻", "inter"))
        self.assertFalse(self.mentions("米兰城德比前瞻", "ac-milan"))

    def test_an_unambiguous_club_alias_is_unaffected(self):
        self.assertTrue(self.mentions("阿森纳客场取胜", "arsenal"))
        self.assertTrue(self.mentions("利物浦补时绝杀", "liverpool"))


if __name__ == "__main__":
    unittest.main()
