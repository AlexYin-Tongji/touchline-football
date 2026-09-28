"""Community sources are attributed, ordered by agreement, never promoted."""
import asyncio
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
import tempfile

from server import provenance as prov
from server.providers import Capability, SourceTier, source, sources
from server.providers import fan_feeds as fan_module
from server.providers import reddit as reddit_module
from server.providers.fan_feeds import FanFeedProvider
from server.providers.reddit import RedditProvider
from server.storage import Store


def thread(title="Someone spotted at the training ground", score=4200, comments=310, permalink="/r/soccer/comments/abc/t/"):
    return {"data": {"children": [{"data": {
        "id": "abc", "title": title, "permalink": permalink, "score": score, "num_comments": comments,
        "created_utc": 1790000000, "link_flair_text": "Injury", "is_self": True,
    }}]}}


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Arseblog</title>
<item><title>Why the midfield changed</title>
<link>https://arseblog.news/p/why-the-midfield-changed</link>
<description>Some analysis.</description>
<pubDate>Sat, 26 Sep 2026 09:00:00 +0000</pubDate></item>
<item><title>Off-site link</title><link>https://elsewhere.example/post</link></item>
</channel></rss>"""


class CommunityCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "app.sqlite3")
        self.addCleanup(self.store.close)

    def read(self, awaitable):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        return loop.run_until_complete(awaitable)


class RegistrationTests(unittest.TestCase):
    def test_both_community_sources_speak_in_the_community_tier(self):
        for name in ("reddit", "fan_feeds"):
            self.assertEqual(source(name).tier, SourceTier.COMMUNITY)

    def test_neither_claims_a_structured_field_capability(self):
        # A forum post must never become a squad number or a transfer status.
        for name in ("reddit", "fan_feeds"):
            capabilities = source(name).capabilities
            self.assertNotIn(Capability.SQUAD, capabilities)
            self.assertNotIn(Capability.STANDINGS, capabilities)

    def test_a_reviewed_feed_host_carries_the_tier(self):
        self.assertEqual(prov.tier_for_url("https://arseblog.news/p/x"), SourceTier.COMMUNITY)

    def test_an_unreviewed_blog_carries_no_tier(self):
        self.assertIsNone(prov.tier_for_url("https://randomblog.example/post"))

    def test_a_community_host_cannot_be_relayed_as_official(self):
        with self.assertRaises(prov.UnregisteredSource):
            prov.claim_from({"id": "reddit", "url": "https://www.reddit.com/r/soccer/x"},
                            "injury", "out", tier=SourceTier.OFFICIAL)

    def test_community_sources_appear_after_dataset_sources(self):
        order = [row.id for row in sources(capability=Capability.NEWS)]
        self.assertEqual(order, ["espn", "fan_feeds", "reddit"])


class RedditReadingTests(CommunityCase):
    def setUp(self):
        super().setUp()
        self.provider = RedditProvider(store=self.store)

    def test_a_thread_becomes_an_attributed_claim(self):
        items, gaps = self.provider._read(thread(), "arsenal")
        self.assertEqual(gaps, [])
        claim = items[0]["claim"]
        self.assertEqual(claim.source_id, "reddit")
        self.assertEqual(claim.tier, SourceTier.COMMUNITY)
        self.assertEqual(claim.value, "Someone spotted at the training ground")

    def test_agreement_is_recorded_and_does_not_change_the_tier(self):
        items, _ = self.provider._read(thread(score=12000, comments=900), "arsenal")
        claim = items[0]["claim"]
        self.assertEqual(claim.corroboration, 12000)
        self.assertEqual(claim.tier, SourceTier.COMMUNITY)
        self.assertIn("社区", claim.relay())

    def test_upvotes_and_comments_are_kept_separate(self):
        items, _ = self.provider._read(thread(score=9, comments=400), "arsenal")
        self.assertEqual(items[0]["score"], 9)
        self.assertEqual(items[0]["comments"], 400)

    def test_a_thread_carries_its_own_timestamp(self):
        items, _ = self.provider._read(thread(), "arsenal")
        self.assertTrue(items[0]["obs_created_at"].startswith("2026-"))
        self.assertNotEqual(items[0]["claim"].observed_at, "")

    def test_offsite_permalinks_are_dropped(self):
        items, _ = self.provider._read(thread(permalink="https://evil.example/x"), "arsenal")
        self.assertEqual(items, [])

    def test_a_changed_response_shape_is_a_retryable_gap(self):
        items, gaps = self.provider._read({"data": {"children": "nope"}}, "arsenal")
        self.assertEqual(items, [])
        self.assertTrue(gaps[0]["retryable"])

    def test_a_missing_score_does_not_invent_one(self):
        items, _ = self.provider._read(thread(score=None), "arsenal")
        self.assertIsNone(items[0]["claim"].corroboration)

    def test_subreddit_membership_alone_does_not_establish_the_club(self):
        unrelated = {"id": "x", "title": "Great goal in the Championship last night", "selftext": ""}
        self.assertFalse(self.provider.verify_identity(unrelated, "arsenal"))

    def test_a_thread_naming_the_club_is_accepted(self):
        related = {"id": "x", "title": "Arsenal team news ahead of the weekend", "selftext": ""}
        self.assertTrue(self.provider.verify_identity(related, "arsenal"))


class FanFeedTests(CommunityCase):
    def setUp(self):
        super().setUp()
        self.provider = FanFeedProvider(store=self.store)

    def test_entries_are_read_with_their_published_time(self):
        entries = fan_module._feed_entries(RSS)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["published_at"], "2026-09-26T09:00:00+00:00")

    def test_a_feed_speaks_for_the_clubs_it_was_reviewed_for(self):
        self.assertTrue(self.provider.feeds_for("arsenal"))
        self.assertEqual(self.provider.feeds_for("wolfsburg"), [])

    def test_an_unreviewed_club_gets_a_gap_not_silence(self):
        answer = self.read(self.provider.entries("wolfsburg"))
        self.assertEqual(answer.items, [])
        self.assertTrue(answer.gaps)
        self.assertFalse(answer.gaps[0]["retryable"])

    def test_an_article_from_another_site_is_dropped(self):
        self.assertFalse(fan_module._same_publication("https://elsewhere.example/post",
                                                      {"host": "arseblog.news"}))
        self.assertTrue(fan_module._same_publication("https://arseblog.news/p/x",
                                                     {"host": "arseblog.news"}))

    def test_a_feed_carries_no_invented_corroboration(self):
        entry = fan_module._feed_entries(RSS)[0]
        self.assertNotIn("score", entry)


class TierLanguageTests(unittest.TestCase):
    def test_a_community_claim_is_relayed_as_a_community_claim(self):
        claim = prov.Claim(field="injury", value="out", source_id="reddit", tier=SourceTier.COMMUNITY,
                           fetched_at=prov.iso(None), corroboration=1)
        self.assertIn("社区", claim.relay())

    def test_an_official_claim_is_relayed_plainly(self):
        claim = prov.Claim(field="injury", value="out", source_id="arsenal_official",
                           tier=SourceTier.OFFICIAL, fetched_at=prov.iso(None))
        self.assertEqual(claim.relay(), "官方")


if __name__ == "__main__":
    unittest.main()
