"""The Chinese-name pass: what it reads, and what it refuses.

A reading is only ever read off a source, and the source has to be about the
right person. The identity comparisons are the part worth testing: a name
attached to the wrong player is worse than an English one, because the reader
has no way to tell it is wrong.

The other half is the *route*. Wikidata's ``zh`` label and the Chinese Wikipedia
article title follow different naming conventions — measured on a real squad,
the label gave 马田·达尼亚 for Martin Terrier where the article title gives
马丁·泰里耶 — so which route produced a reading has to be recorded, and the
preference has to be for the article title.

No network here. Every response is a fixture, so the tests state what the
resolver does with a given answer rather than what the live APIs happen to say.
"""
import json
import unittest
from unittest.mock import patch

from server import team_names
from server.player_names import NAME_SOURCES, checked_wikidata_reading
from tests.test_agent import FakeStore
from tests.test_workflows import MemoryStore


PLAYER = {"name": "Martin Terrier", "birth_date": "1997-03-04", "provider_id": "241662",
          "team_id": "bayer-leverkusen"}


def search(*titles):
    return {"query": {"search": [{"title": title} for title in titles]}}


def langlinks(title, zh_title=None):
    page = {"title": title}
    if zh_title:
        page["langlinks"] = [{"lang": "zh", "*": zh_title}]
    return {"query": {"pages": {"1": page}}}


def pageprops(title, qid):
    return {"query": {"pages": {"1": {"title": title, "pageprops": {"wikibase_item": qid}}}}}


def article(title, extract, short="association football player"):
    return {"query": {"pages": {"1": {"title": title,
                                      "pageprops": {"wikibase-shortdesc": short},
                                      "extract": extract}}}}


def labels(qid, **by_language):
    return {"entities": {qid: {"id": qid, "labels": {lang: {"language": lang, "value": value}
                                                     for lang, value in by_language.items()}}}}


BORN = "Martin Terrier (born 4 March 1997) is a French professional footballer who plays as a forward for Bayer Leverkusen."


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeStore()
        self.replies = []
        self.seen_hosts = []

    async def respond(self, store, host, params):
        self.seen_hosts.append((host, params.get("action")))
        if not self.replies:
            raise AssertionError(f"unexpected request: {host} {params}")
        return self.replies.pop(0)

    async def resolve(self):
        with patch.object(team_names, "_load", new=self.respond):
            return await team_names.resolve_reading(self.store, PLAYER, "bayer-leverkusen")

    async def test_the_chinese_wikipedia_title_is_preferred_over_the_label(self):
        # Both routes are available and they disagree. The article title follows
        # the Mainland convention, so it is the one published.
        self.replies = [search("Martin Terrier", "Martin Terrier (footballer, born 1992)"),
                        article("Martin Terrier", BORN),
                        pageprops("Martin Terrier", "Q27978968"),
                        langlinks("Martin Terrier", "马丁·泰里耶"),
                        labels("Q27978968", zh="馬田·達尼亞")]
        record, reason = await self.resolve()
        self.assertIsNone(reason)
        self.assertEqual(record["name_zh"], "马丁·泰里耶")
        self.assertEqual(record["name_zh_source"], "维基百科中文条目")
        self.assertEqual(record["name_zh_source_url"], "https://zh.wikipedia.org/wiki/马丁·泰里耶")
        # The proof must satisfy the verifier it was written for.
        from server.player_names import checked_wikidata_reading
        self.assertIsNotNone(checked_wikidata_reading(record, {**PLAYER}, team_id="bayer-leverkusen"))

    async def test_the_wikidata_label_is_used_when_there_is_no_chinese_article(self):
        self.replies = [search("Martin Terrier"),
                        article("Martin Terrier", BORN),
                        pageprops("Martin Terrier", "Q27978968"),
                        langlinks("Martin Terrier", None),
                        labels("Q27978968", zh="馬田·達尼亞")]
        record, reason = await self.resolve()
        self.assertIsNone(reason)
        # Traditional converted one-to-one; the published form stays in the proof.
        self.assertEqual(record["name_zh"], "马田·达尼亚")
        self.assertEqual(record["name_zh_source"], "Wikidata 中文标签")
        from server.player_names import checked_wikidata_reading
        self.assertIsNotNone(checked_wikidata_reading(record, {**PLAYER}, team_id="bayer-leverkusen"))
        self.assertEqual(record["name_zh_identity"]["label"], "馬田·達尼亞")

    async def test_a_whole_year_birth_date_mismatch_is_refused(self):
        # The namesake case: same name, different person. The article title even
        # carries the other man's year, which is what makes it look like a hit.
        self.replies = [search("Martin Terrier", "Martin Terrier (footballer, born 1992)"),
                        article("Martin Terrier", "Martin Terrier (born 14 July 1992) is a German footballer.")]
        record, reason = await self.resolve()
        self.assertIsNone(record)
        self.assertEqual(reason, "birth_date_mismatch")

    async def test_a_hit_whose_title_disagrees_with_the_full_name_is_not_a_candidate(self):
        self.replies = [search("Martin Terrier (footballer, born 1992)")]
        record, reason = await self.resolve()
        self.assertIsNone(record)
        self.assertEqual(reason, "no_matching_article")

    async def test_a_non_footballer_with_the_same_name_is_refused(self):
        self.replies = [search("Martin Terrier"),
                        article("Martin Terrier", "Martin Terrier (born 4 March 1997) is a French actor.",
                                short="French actor")]
        # The occupation check reads the mixed description text, so this only
        # passes the article step if neither field mentions football.
        record, reason = await self.resolve()
        self.assertIsNone(record)
        self.assertEqual(reason, "not_a_footballer")

    async def test_an_article_about_another_club_is_refused(self):
        self.replies = [search("Martin Terrier"),
                        article("Martin Terrier", "Martin Terrier (born 4 March 1997) is a French footballer who plays for Olympique Lyonnais.")]
        record, reason = await self.resolve()
        self.assertIsNone(record)
        self.assertEqual(reason, "other_club")

    async def test_a_missing_birth_date_cannot_be_checked_so_nothing_is_read(self):
        record, reason = await team_names.resolve_reading(
            self.store, {**PLAYER, "birth_date": ""}, "bayer-leverkusen")
        self.assertIsNone(record)
        self.assertEqual(reason, "missing_birth_date")
        self.assertEqual(self.seen_hosts, [], "an uncheckable row must not reach the network")

    async def test_neither_route_having_a_reading_is_reported_as_such(self):
        self.replies = [search("Martin Terrier"),
                        article("Martin Terrier", BORN),
                        pageprops("Martin Terrier", "Q27978968"),
                        langlinks("Martin Terrier", None),
                        labels("Q27978968")]
        record, reason = await self.resolve()
        self.assertIsNone(record)
        self.assertEqual(reason, "no_chinese_reading")

    async def test_a_transport_fault_never_reads_as_the_source_having_no_name(self):
        with patch.object(team_names, "_load", new=self.respond):
            with self.assertRaises(Exception):
                await team_names.resolve_reading(self.store, PLAYER, "bayer-leverkusen")


class CacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # set_meta/get_meta is the metadata layer the reading cache lives in.
        self.store = MemoryStore()

    def record(self, **overrides):
        proof = {"qid": "Q27978968", "label": "马丁·泰里耶", "published_label": "马丁·泰里耶",
                 "zh_title": "马丁·泰里耶",
                 "language": "zh", "method": "wikidata_existing_identity_check", "name": "Martin Terrier",
                 "team_id": "bayer-leverkusen", "roster_birth_date": "1997-03-04",
                 "provider_id": "241662", "unique_article": True, "identity_checked": True, "public": True}
        value = {"name_status": "verified", "name_zh": "马丁·泰里耶", "name_zh_source": "维基百科中文条目",
                 "name_zh_source_url": "https://zh.wikipedia.org/wiki/马丁·泰里耶",
                 "name_zh_checked_at": "2026-10-13T00:00:00+00:00", "name_zh_identity": proof,
                 "checked_at": "2026-10-13T00:00:00+00:00"}
        value.update(overrides)
        return value

    def store_it(self, record):
        team_names.store_reading(self.store, PLAYER, "bayer-leverkusen", record)
        return team_names.cached_reading(self.store, PLAYER, "bayer-leverkusen")

    def test_a_stored_reading_comes_back_for_the_same_identity(self):
        read = self.store_it(self.record())
        self.assertIsNotNone(read)
        self.assertEqual(read["name_zh"], "马丁·泰里耶")

    def test_a_changed_provider_id_invalidates_the_proof(self):
        # Rebuilding the roster can change the provider id. The proof was tied to
        # the old one, so it is dropped rather than displayed for a row it was
        # never checked against.
        team_names.store_reading(self.store, PLAYER, "bayer-leverkusen", self.record())
        moved = {**PLAYER, "provider_id": "999999"}
        self.assertIsNone(team_names.cached_reading(self.store, moved, "bayer-leverkusen"))

    def test_a_reading_that_outlived_its_window_is_not_returned(self):
        self.store_it(self.record(name_zh_checked_at="2020-01-01T00:00:00+00:00",
                                  checked_at="2020-01-01T00:00:00+00:00"))
        self.assertIsNone(team_names.cached_reading(self.store, PLAYER, "bayer-leverkusen"))

    def test_every_name_source_is_a_fixed_pair_of_label_and_url_shape(self):
        # The verifier must not accept a free-text source. Each entry names both
        # what the source is called and what its citation looks like, and an
        # unknown name has no entry to match.
        self.assertEqual(set(NAME_SOURCES), {"Wikidata 中文标签", "维基百科中文条目"})
        for name, entry in NAME_SOURCES.items():
            with self.subTest(source=name):
                self.assertTrue(entry["url"]({"qid": "Q1", "zh_title": "某球员"}).startswith("https://"))
                self.assertTrue(entry["method"])

    def test_a_source_name_outside_the_table_is_refused(self):
        row = {**PLAYER}
        record = self.record(name_zh_source="某站", name_zh_source_url="https://example.com/wiki/Q27978968")
        self.assertIsNone(checked_wikidata_reading(record, row, team_id="bayer-leverkusen"))

    def test_a_citation_url_that_does_not_match_its_source_is_refused(self):
        row = {**PLAYER}
        record = self.record(name_zh_source="维基百科中文条目",
                             name_zh_source_url="https://www.wikidata.org/wiki/Q27978968")
        self.assertIsNone(checked_wikidata_reading(record, row, team_id="bayer-leverkusen"))


if __name__ == "__main__":
    unittest.main()
