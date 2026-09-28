"""A reader can tell a club's own site apart from a supporters' forum."""
import unittest

from server.personal_app import TIER_LABELS, UNCLASSIFIED_LABEL, source_tier


class SourceTierLabelTests(unittest.TestCase):
    def test_a_reviewed_official_host_is_labelled_official(self):
        # Arsenal is reached through a reviewed club source in the catalog.
        self.assertIn("tier", source_tier("https://www.arsenal.com/news/men/1"))

    def test_a_community_host_is_labelled_community(self):
        label = source_tier("https://www.reddit.com/r/soccer/comments/abc/t/")
        self.assertEqual(label["tier"], "community")
        self.assertEqual(label["tier_label"], "球迷社区")
        self.assertTrue(label["tier_known"])

    def test_a_dataset_host_is_labelled_a_dataset(self):
        label = source_tier("https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1")
        self.assertEqual(label["tier"], "dataset")

    def test_an_unreviewed_host_is_not_given_a_tier(self):
        # Defaulting to any tier would let an unreviewed blog sit next to the
        # club's own site looking equally authoritative.
        label = source_tier("https://some-unreviewed-blog.example/post")
        self.assertIsNone(label["tier"])
        self.assertFalse(label["tier_known"])
        self.assertEqual(label["tier_label"], UNCLASSIFIED_LABEL)

    def test_an_invalid_url_does_not_raise(self):
        for value in ("", "not a url", "javascript:alert(1)", None):
            self.assertFalse(source_tier(value)["tier_known"])

    def test_every_label_is_distinct(self):
        self.assertEqual(len(set(TIER_LABELS.values())), len(TIER_LABELS))

    def test_no_label_reads_as_an_endorsement(self):
        # The wording tells a reader how much weight to give a line, so it must
        # not imply that anything was verified.
        for label in {*TIER_LABELS.values(), UNCLASSIFIED_LABEL}:
            for word in ("已核实", "已确认", "可信", "官方认证"):
                self.assertNotIn(word, label)


if __name__ == "__main__":
    unittest.main()
