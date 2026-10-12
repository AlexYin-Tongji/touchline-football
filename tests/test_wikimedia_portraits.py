"""A free image is usable only after person, current club and file checks."""
import asyncio
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server import community_portraits, team_portraits, wikimedia_portraits
from server.ingestion import now
from server.storage import Store


def page(title="Martin Ødegaard", club="Arsenal", birthday="17 December 1998", image="Martin.jpg"):
    return {"query": {"pages": {"1": {"title": title,
        "pageprops": {"wikibase-shortdesc": "Norwegian footballer (born 1998)", "wikibase_item": "Q16902219"},
        "extract": f"{title} (born {birthday}) is a Norwegian professional footballer who plays for Premier League club {club}.\nEarlier career elsewhere.",
        "pageimage": image, "thumbnail": {"source": "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/Martin.jpg/300px-Martin.jpg"}}}}}


def commons(license_name="CC BY-SA 4.0", restrictions="personality", image="Martin.jpg"):
    return {"query": {"pages": {"2": {"title": "File:" + image, "imageinfo": [{
        "thumburl": "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/Martin.jpg/300px-Martin.jpg",
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Martin.jpg",
        "extmetadata": {"LicenseShortName": {"value": license_name}, "License": {"value": "cc-by-sa-4.0"},
            "Artist": {"value": "<a>Bryan Berlin</a>"}, "Restrictions": {"value": restrictions}}}]}}}}


class WikimediaPortraitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.player = {"name": "Martin Ødegaard", "birth_date": "1998-12-17"}
        # These tests exercise the Wikimedia resolver. The community fallback
        # that runs after it in the same batch must never touch the network
        # here, so its transport is refused for the whole class.
        offline = patch.object(community_portraits, "fetch_document",
                               side_effect=AssertionError("community source must not be contacted"))
        offline.start()
        self.addCleanup(offline.stop)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def community_silent(self, status="missing"):
        """State the community source's verdict instead of letting it answer.

        A roster entry that carries a birth date is now asked of the community
        publisher too, because a birth date alone can confirm a match. These
        tests are about the Wikimedia verdict, so the other source's answer is
        declared here rather than left to decide the outcome.
        """
        return patch.object(community_portraits, "resolve_portrait", new_callable=AsyncMock,
                            return_value={"status": status, "photo_reason": "社区球员库没有这名球员的可展示照片。",
                                          "retryable": False, "reason_code": "community_" + status})

    def label_reply(self, label="马丁·厄德高"):
        """The entity's own labels, as ``_wikidata_label`` asks for them."""
        return {"entities": {"Q16902219": {
            "id": "Q16902219",
            "labels": {"zh-hans": {"language": "zh-hans", "value": label}}}}}

    def replies(self, articles=None, license_result=None):
        articles = articles if articles is not None else [page()]
        license_result = license_result if license_result is not None else commons()
        async def answer(store, host, params):
            if params.get("list") == "search":
                return {"query": {"search": [{"title": a["query"]["pages"]["1"]["title"]} for a in articles]}}
            if host == "www.wikidata.org":
                return self.label_reply()
            if host == "commons.wikimedia.org":
                return license_result
            title = params.get("titles")
            return next((a for a in articles if a["query"]["pages"]["1"]["title"] == title), {})
        return answer

    def identity_replies(self, *, label="马丁·厄德高", image="", entity_flags=None, license_result=None):
        article = page(image=image)
        article["query"]["pages"]["1"]["extract"] = "Martin Ødegaard (born 17 December 1998) is a Norwegian professional footballer."
        entity = {"id": "Q16902219", "claims": {
            "P106": [{"mainsnak": {"datavalue": {"value": {"id": "Q937857"}}}}],
            "P569": [{"mainsnak": {"datavalue": {"value": {"time": "+1998-12-17T00:00:00Z"}}}}],
            "P54": [{"mainsnak": {"datavalue": {"value": {"id": "Q9617"}}},
                     "qualifiers": {"P580": [{"datavalue": {"value": {"time": "+2020-01-01T00:00:00Z"}}}]}}]},
            "labels": {"zh-hans": {"language": "zh-hans", "value": label}}}
        entity.update(entity_flags or {})
        async def answer(store, host, params):
            if host == "www.wikidata.org":
                if params.get("ids") == "Q16902219":
                    # Two callers reach this entity: the club check asks for
                    # claims and labels together, while a player whose article
                    # already settled the club is asked only for the label.
                    if params.get("props") == "claims|labels":
                        self.assertEqual(params["languages"], "zh-hans|zh")
                    else:
                        self.assertEqual(params["props"], "labels")
                    return {"entities": {"Q16902219": entity}}
                return {"entities": {"Q9617": {"labels": {"en": {"value": "Arsenal F.C."}}}}}
            if params.get("list") == "search":
                return {"query": {"search": [{"title": "Martin Ødegaard"}]}}
            if host == "commons.wikimedia.org":
                return license_result if license_result is not None else commons()
            return article
        return answer

    async def test_existing_identity_request_can_supply_chinese_name_without_a_photo(self):
        player = {**self.player, "provider_id": "101", "team_id": "arsenal"}
        with patch.object(wikimedia_portraits, "request_json", side_effect=self.identity_replies()) as fetch:
            result = await wikimedia_portraits.resolve_portrait(self.store, player, "arsenal")
        self.assertEqual(fetch.call_count, 4, "search, article, existing person identity and existing club label requests")
        self.assertEqual(result["status"], "missing")
        self.assertFalse(result.get("photo_url"))
        self.assertEqual(result["name_status"], "verified")
        self.assertEqual(result["name_zh"], "马丁·厄德高")
        self.assertEqual(result["name_zh_source_url"], "https://www.wikidata.org/wiki/Q16902219")
        snapshot = {"team_id": "arsenal", "roster": [dict(player, photo_url="")]}
        shown = team_portraits.apply_cached_portraits(self.store, snapshot)["roster"][0]
        self.assertEqual(shown["name_zh"], "马丁·厄德高")
        self.assertEqual(shown["name"], player["name"])
        self.assertTrue(shown["name_zh_identity"]["identity_checked"])
        self.assertFalse(shown["photo_url"])
        changed_person = {**player, "provider_id": "102"}
        self.assertIsNone(wikimedia_portraits.cached_reading(self.store, changed_person, "arsenal"))

    async def test_name_identity_and_photo_license_have_independent_outcomes(self):
        player = {**self.player, "provider_id": "101", "team_id": "arsenal"}
        with patch.object(wikimedia_portraits, "request_json", side_effect=self.identity_replies(
                image="Martin.jpg", license_result=commons("CC BY-NC 4.0"))) as fetch:
            result = await wikimedia_portraits.resolve_portrait(self.store, player, "arsenal")
        self.assertEqual(fetch.call_count, 5)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(wikimedia_portraits.cached_reading(self.store, player, "arsenal")["name_zh"], "马丁·厄德高")
        self.assertFalse(result.get("photo_url"))

    async def test_photo_license_network_failure_cannot_erase_a_checked_name(self):
        from server.ingestion import IngestionError
        player = {**self.player, "provider_id": "101", "team_id": "arsenal"}
        with patch.object(wikimedia_portraits, "request_json", side_effect=self.identity_replies(image="Martin.jpg")), \
             patch.object(wikimedia_portraits, "_commons_license", new=AsyncMock(side_effect=IngestionError("许可来源暂时不可达"))):
            result = await wikimedia_portraits.resolve_portrait(self.store, player, "arsenal")
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result.get("photo_url"))
        self.assertEqual(wikimedia_portraits.cached_reading(self.store, player, "arsenal")["name_zh"], "马丁·厄德高")

    async def test_missing_private_or_non_chinese_entity_cannot_publish_a_name(self):
        player = {**self.player, "provider_id": "101", "team_id": "arsenal"}
        for flags, label in (({}, "Martin Odegaard"), ({"missing": ""}, "马丁·厄德高"),
                             ({"private": True}, "马丁·厄德高"), ({"deleted": True}, "马丁·厄德高"),
                             ({"id": "Q123"}, "马丁·厄德高"), ({}, "马丁<script>")):
            with self.subTest(flags=flags, label=label), patch.object(wikimedia_portraits, "request_json",
                    side_effect=self.identity_replies(label=label, entity_flags=flags)):
                result = await wikimedia_portraits.resolve_portrait(self.store, player, "arsenal", force=True)
            self.assertNotIn("name_zh", result)
            self.assertIsNone(wikimedia_portraits.cached_reading(self.store, player, "arsenal"))

    async def test_latest_unknown_name_proof_cannot_restore_previous_name_fields(self):
        player = {**self.player, "provider_id": "101", "team_id": "arsenal"}
        with patch.object(wikimedia_portraits, "request_json", side_effect=self.identity_replies()):
            verified = await wikimedia_portraits.resolve_portrait(self.store, player, "arsenal")
        shown = team_portraits.apply_cached_portraits(self.store, {"team_id": "arsenal", "roster": [dict(player)]})
        # The identity-checked label is longer than the table's reading and
        # contains it, so the full published name is shown with its citation.
        self.assertEqual(shown["roster"][0]["name_zh"], "马丁·厄德高")
        self.store.set_meta(wikimedia_portraits._portrait_key(player, "arsenal"),
                            {"status": "unavailable", "name_status": "unknown", "last_success": verified})
        revoked = team_portraits.apply_cached_portraits(self.store, shown)
        # The revoked label and its proof must not survive. The table's own
        # reading is a separate fact and does survive: withdrawing a Wikidata
        # label does not un-write the name table.
        self.assertNotIn("name_zh_identity", revoked["roster"][0])
        self.assertNotEqual(revoked["roster"][0].get("name_zh_source"), "Wikidata 中文标签")
        self.assertEqual(revoked["roster"][0]["name_zh"], "厄德高")

    async def test_ambiguous_person_pages_cannot_publish_either_chinese_label(self):
        player = {**self.player, "provider_id": "101", "team_id": "arsenal"}
        base = self.identity_replies()
        async def ambiguous(store, host, params):
            if params.get("list") == "search":
                return {"query": {"search": [{"title": "Martin Ødegaard"}, {"title": "Martin Ødegaard (footballer)"}]}}
            response = await base(store, host, params)
            if params.get("titles") == "Martin Ødegaard (footballer)":
                response["query"]["pages"]["1"]["title"] = params["titles"]
            return response
        with patch.object(wikimedia_portraits, "request_json", side_effect=ambiguous):
            result = await wikimedia_portraits.resolve_portrait(self.store, player, "arsenal")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["name_status"], "unknown")
        self.assertNotIn("name_zh", result)
        self.assertIsNone(wikimedia_portraits.cached_reading(self.store, player, "arsenal"))

    async def test_exact_current_club_and_free_license_keep_author_and_personality_notice(self):
        with patch.object(wikimedia_portraits, "request_json", side_effect=self.replies()) as fetch:
            result = await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal")
            again = await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal")
        self.assertEqual(result["photo_match"], "wikimedia_verified_club")
        self.assertEqual(result["photo_license"], "CC BY-SA 4.0")
        self.assertEqual(result["photo_credit"], "Bryan Berlin")
        self.assertEqual(result["photo_restrictions"], "personality")
        self.assertEqual(result["photo_file_page"], "https://commons.wikimedia.org/wiki/File:Martin.jpg")
        self.assertEqual(result, again)
        # search, article, the entity's label (the article's first sentence
        # already settled the club) and the Commons licence. The second call is
        # answered from cache.
        self.assertEqual(fetch.call_count, 4)

    async def test_namesake_other_current_club_is_rejected_even_if_history_names_target(self):
        wrong = page("Emiliano Martínez", club="Chelsea", birthday="2 September 1992")
        wrong["query"]["pages"]["1"]["extract"] += "\nPreviously, he played for Aston Villa."
        other = page("Emiliano Martínez (footballer, born 1999)", club="Midtjylland", birthday="17 August 1999")
        player = {"name": "Emiliano Martínez", "birth_date": "1992-09-02"}
        with patch.object(wikimedia_portraits, "request_json", side_effect=self.replies([wrong, other])) as fetch:
            result = await wikimedia_portraits.resolve_portrait(self.store, player, "aston-villa")
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result.get("photo_url"))
        self.assertIn("俱乐部", result["photo_reason"])
        self.assertEqual(fetch.call_count, 2)

    async def test_missing_image_wrong_birthday_and_nonfootball_page_never_reach_commons(self):
        nonfootball = page()
        nonfootball["query"]["pages"]["1"]["pageprops"]["wikibase-shortdesc"] = "Norwegian cyclist"
        nonfootball["query"]["pages"]["1"]["extract"] = "Martin Ødegaard is a Norwegian cyclist."
        for candidate in (page(image=""), page(birthday="1 January 1999"), nonfootball):
            record = candidate["query"]["pages"]["1"]
            if record.get("pageimage") == "":
                record.pop("pageimage")
            with self.subTest(record=record), patch.object(wikimedia_portraits, "request_json", side_effect=self.replies([candidate])) as fetch:
                result = await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal")
                self.assertFalse(result.get("photo_url"))
                # At most search, article and the entity label: the point of
                # this test is that none of these three reaches Commons.
                self.assertLessEqual(fetch.call_count, 3)
                self.assertFalse(any(call.args[1] == "commons.wikimedia.org" for call in fetch.call_args_list))

    async def test_nonfree_or_unavailable_license_fails_closed(self):
        for license_result in (commons("CC BY-NC 4.0"), {"query": {"pages": {"2": {"title": "File:Martin.jpg"}}}}):
            with self.subTest(license_result=license_result), patch.object(wikimedia_portraits, "request_json", side_effect=self.replies(license_result=license_result)):
                result = await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal")
                self.assertFalse(result.get("photo_url"))

    async def test_force_rechecks_cached_commons_license(self):
        with patch.object(wikimedia_portraits, "request_json", side_effect=self.replies()):
            original = await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal")
        self.assertEqual(original["status"], "completed")
        with patch.object(wikimedia_portraits, "request_json",
                          side_effect=self.replies(license_result=commons("CC BY-NC 4.0"))) as fetch:
            updated = await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal", force=True)
        self.assertEqual(updated["status"], "rejected")
        self.assertFalse(updated.get("photo_url"))
        # search, article, label, licence — the forced pass re-asks the source
        # rather than replaying the cached verdict.
        self.assertEqual(fetch.call_count, 4)

    async def test_article_redirect_to_other_person_and_file_title_punctuation_mismatch_fail_closed(self):
        wrong_article = page("Different Player")
        async def mismatch_article(store, host, params):
            if params.get("list") == "search":
                return {"query": {"search": [{"title": "Martin Ødegaard"}]}}
            return wrong_article
        with patch.object(wikimedia_portraits, "request_json", side_effect=mismatch_article) as fetch:
            result = await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal")
        self.assertFalse(result.get("photo_url"))
        self.assertEqual(fetch.call_count, 2)
        self.assertIsNone(wikimedia_portraits._license_metadata(
            commons(image="Martin,Jr.jpg"), "Martin Jr.jpg"))

    async def test_offline_and_cancel_never_publish_uncorroborated_photo(self):
        with patch.object(wikimedia_portraits, "request_json", side_effect=ValueError("offline")):
            result = await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal")
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result.get("photo_url"))
        self.player["name"] = "Another Player"
        with patch.object(wikimedia_portraits, "request_json", new_callable=AsyncMock, side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal")

    async def test_team_flow_uses_bounded_batches_and_advances_cursor(self):
        # Two full batches plus one leftover player, so the bound and the
        # cursor advance are both exercised regardless of the batch size.
        size = team_portraits.PLAYER_PAGE_LIMIT * 2 + 1
        squad = {"team_id": "arsenal", "season_id": "eng.1:2026", "roster": [
            {"id": f"espn-player-{i}", "provider_id": str(i), "name": f"Player {i}",
             "birth_date": "", "number": str(i), "photo_url": ""} for i in range(1, size + 1)]}
        async def missing(store, player, team_id):
            result = {"status": "missing", "photo_reason": "没有自由照片。", "fetched_at": now()}
            store.set_meta(wikimedia_portraits._portrait_key(player, team_id), result)
            return result
        with patch.object(wikimedia_portraits, "resolve_portrait", side_effect=missing) as resolve:
            first, summary1 = await team_portraits.enrich_player_photos(self.store, squad)
            second, summary2 = await team_portraits.enrich_player_photos(self.store, first)
        self.assertEqual(summary1["attempted_count"], team_portraits.PLAYER_PAGE_LIMIT)
        self.assertEqual(summary2["attempted_count"], team_portraits.PLAYER_PAGE_LIMIT)
        self.assertEqual(summary2["remaining_count"], 1)
        self.assertEqual([call.args[1]["provider_id"] for call in resolve.call_args_list],
                         [str(i) for i in range(1, size)])
        self.assertEqual(second["roster"][0]["photo_reason"], "没有自由照片。")

    async def test_force_identity_rejection_removes_old_wikimedia_portrait(self):
        squad = {"team_id": "arsenal", "season_id": "eng.1:2026", "roster": [{
            "id": "espn-player-1", "provider_id": "1", "name": "Martin Ødegaard",
            "birth_date": "1998-12-17", "number": "8",
            "photo_url": "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/Martin.jpg/300px-Martin.jpg",
            "photo_match": "wikimedia_verified_club", "photo_license": "CC BY-SA 4.0",
            "photo_file_page": "https://commons.wikimedia.org/wiki/File:Martin.jpg"}]}
        # Both sources are asked, so both are stubbed: this test is about the
        # Wikimedia verdict, and letting the community source reach the network
        # would make its answer decide the outcome instead.
        with self.community_silent(), \
                patch.object(wikimedia_portraits, "resolve_portrait", new_callable=AsyncMock,
                             return_value={"status": "rejected", "photo_reason": "页面记载的当前俱乐部与阵容不一致。"}):
            result, summary = await team_portraits.enrich_player_photos(self.store, squad, force=True)
        self.assertFalse(result["roster"][0]["photo_url"])
        self.assertFalse(result["roster"][0].get("photo_file_page"))
        self.assertEqual(summary["status"], "missing")

    async def test_community_source_outage_stops_early_and_independent_fallback_recovers(self):
        """The community source is read first; its outage must not end the pass.

        The measured order puts the community publisher first because it answers
        for more players and costs a fraction of the Wikimedia lookups. That
        makes Wikimedia the *independent fallback*: when the community source is
        unavailable, the players it could not answer still have somewhere to go,
        and one outage must not be recorded as "these players have no photo".

        The stub writes the cache entry the real resolver writes, because a
        source's own cache is what the read path consults: a stub that only
        returns a dict would let the row be filled and then cleared again by the
        end-of-pass recheck, which is not what happens in production.
        """
        squad = {"team_id": "arsenal", "roster": [{"provider_id": str(i), "name": f"Player {i}",
                    "nationality": "英格兰", "photo_url": ""} for i in (1, 2)]}
        unavailable = {"status": "unavailable", "retryable": True, "photo_reason": "社区球员库暂不可达"}
        photo = {"status": "completed", "photo_url": "https://thumb.wikimedia.org/wikipedia/commons/a.png",
                 "photo_credit": "Author", "photo_source_url": "https://en.wikipedia.org/wiki/Player",
                 "photo_file_page": "https://commons.wikimedia.org/wiki/File:A.png",
                 "photo_license": "CC BY-SA 4.0", "photo_match": "wikimedia_verified_club",
                 "fetched_at": now(), "photo_reason": ""}
        async def resolved(store, player, team_id, **kwargs):
            # Base key only: the product's own _portrait_key hashes the roster
            # name and birth date, which the stub does not need to reproduce.
            result = dict(photo, last_attempt=now())
            store.set_meta(wikimedia_portraits._portrait_key(player, team_id), result)
            return result
        with patch.object(community_portraits, "resolve_portrait", new=AsyncMock(return_value=unavailable)) as primary, \
             patch.object(wikimedia_portraits, "resolve_portrait", side_effect=resolved) as fallback:
            result, summary = await team_portraits.enrich_player_photos(self.store, squad)
        self.assertEqual(primary.await_count, 1, "the first outage stops the rest of this source's batch")
        self.assertEqual(fallback.await_count, 2, "the independent source still answers both players")
        self.assertEqual(summary["selected_available_count"], 2)
        self.assertEqual(summary["errors"], [])
        for row in result["roster"]:
            self.assertTrue(row["photo_url"])
            self.assertEqual(row["photo_match"], "wikimedia_verified_club")
            self.assertFalse(row.get("reason_code"))
            self.assertTrue(row["photo_license"] and row["photo_file_page"])

    async def test_outage_fallback_is_bounded_and_advances_its_own_cursor(self):
        """Each source keeps its own bound and its own cursor.

        The community publisher is asked first, so its batch is the one bounded
        by COMMUNITY_PAGE_LIMIT and whose cursor advances across passes. The
        Wikimedia half now runs only for the players left over, and its outage
        must not stall the community source's next batch.
        """
        size = team_portraits.COMMUNITY_PAGE_LIMIT * 2 + 1
        squad = {"team_id": "arsenal", "season_id": "PL-2026", "roster": [
            {"provider_id": str(i), "name": f"Player {i}", "photo_url": ""} for i in range(1, size + 1)]}
        async def missing(store, row, team_id, **kwargs):
            result = {"status": "missing", "fetched_at": now(), "photo_reason": "社区库里没有这名球员。"}
            store.set_meta(community_portraits.portrait_key(row, team_id), result)
            return result
        async def unavailable(store, row, team_id, **kwargs):
            result = {"status": "unavailable", "last_attempt": now(), "photo_reason": "维基媒体稍后重试", "retryable": True}
            store.set_meta(wikimedia_portraits._portrait_key(row, team_id), result)
            return result
        with patch.object(community_portraits, "resolve_portrait", side_effect=missing) as primary, \
             patch.object(wikimedia_portraits, "resolve_portrait", side_effect=unavailable) as fallback:
            first, one = await team_portraits.enrich_player_photos(self.store, squad)
            _, two = await team_portraits.enrich_player_photos(self.store, first)
        self.assertEqual(one["community_attempted_count"], team_portraits.COMMUNITY_PAGE_LIMIT)
        self.assertEqual(two["community_attempted_count"], team_portraits.COMMUNITY_PAGE_LIMIT)
        self.assertTrue(one["continuation_ready"], "an independent Wikimedia outage must not stall the healthy source's next batch")
        self.assertTrue(two["continuation_ready"])
        self.assertEqual([call.args[1]["provider_id"] for call in primary.call_args_list],
                         [str(i) for i in range(1, size)])
        # The fallback still runs, bounded by its own limit, for the players the
        # first source could not answer.
        self.assertGreaterEqual(fallback.await_count, 1)
        self.assertLessEqual(fallback.await_count, 2 * team_portraits.PLAYER_PAGE_LIMIT)

    async def test_both_source_failures_are_reported_and_survive_cache_replay(self):
        """Two independent outages are two facts, and both are kept.

        The community publisher is asked first and its transport fault sets the
        row's `reason_code`. Wikimedia then fails for the same player. The old
        assertion required the second failure to be *absent* from the message,
        which only made sense when one source was nominated as "the" source;
        with both able to answer, hiding the other's fault would tell the reader
        one source is down when two are.
        """
        row = {"provider_id": "1", "name": "Kai Havertz", "nationality": "德国", "photo_url": ""}
        primary_gap = {"status": "unavailable", "photo_reason": "社区球员库连接失败，稍后重试。",
                       "reason_code": "connection_error", "retryable": True, "last_attempt": now()}
        fallback_gap = {"status": "unavailable", "photo_reason": "维基媒体请求过于频繁，稍后重试。",
                        "reason_code": "http_rate_limited", "retryable": True, "last_attempt": now()}
        async def primary(store, player, team_id, **kwargs):
            store.set_meta(community_portraits.portrait_key(player, team_id), primary_gap)
            return primary_gap
        async def fallback(store, player, team_id, **kwargs):
            store.set_meta(wikimedia_portraits._portrait_key(player, team_id), fallback_gap)
            return fallback_gap
        with patch.object(community_portraits, "resolve_portrait", side_effect=primary), \
             patch.object(wikimedia_portraits, "resolve_portrait", side_effect=fallback):
            result, summary = await team_portraits.enrich_player_photos(self.store, {"team_id": "arsenal", "roster": [row]})
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(len(summary["errors"]), 1)
        # The source consulted first leads, because that is the one whose limit
        # stopped the batch; the second source's wording is appended, not
        # swapped in.
        self.assertIn("TheSportsDB", summary["errors"][0])
        self.assertIn(primary_gap["photo_reason"], summary["errors"][0])
        self.assertIn(fallback_gap["photo_reason"], summary["errors"][0])
        player = result["roster"][0]
        self.assertFalse(player["photo_url"])
        self.assertEqual(player["reason_code"], "connection_error")
        self.assertTrue(player["retryable"])
        # Both sources were asked and both failed, so the row says so.
        self.assertIn("维基媒体照片来源", player["photo_reason"])
        self.assertIn("TheSportsDB 社区照片来源", player["photo_reason"])
        self.assertIn(primary_gap["photo_reason"], player["photo_reason"])
        self.assertIn(fallback_gap["photo_reason"], player["photo_reason"])
        # A later read rebuilds the row from whichever source holds a cached
        # verdict, so it reports that source's own reason. That is the contract
        # the read path can honour without a new cross-source record.
        replay = team_portraits.apply_cached_portraits(
            self.store, {"team_id": "arsenal", "roster": [dict(row)]})["roster"][0]
        self.assertFalse(replay["photo_url"])
        self.assertTrue(replay["reason_code"])
        self.assertIn("照片来源", replay["photo_reason"])

    async def test_an_attempted_but_unavailable_source_does_not_mark_a_batch_ready(self):
        squad = {"team_id": "arsenal", "roster": [
            {"provider_id": str(index), "name": f"Player {index}", "nationality": "德国", "photo_url": ""}
            for index in range(1, 15)]}
        gap = {"status": "unavailable", "photo_reason": "暂时不可达", "retryable": True}
        with patch.object(wikimedia_portraits, "resolve_portrait", new=AsyncMock(return_value=gap)), \
             patch.object(community_portraits, "resolve_portrait", new=AsyncMock(return_value=gap)):
            _, summary = await team_portraits.enrich_player_photos(self.store, squad)
        self.assertEqual(summary["community_attempted_count"], 1)
        self.assertGreater(summary["community_remaining_count"], 0)
        self.assertTrue(summary["community_unavailable"])
        self.assertFalse(summary["continuation_ready"])

    async def test_old_roster_community_photo_cannot_bypass_new_identity_check(self):
        old_photo = {"status": "completed", "photo_url": "https://r2.thesportsdb.com/images/media/player/cutout/old.png",
                     "photo_source_url": "https://www.thesportsdb.com/player/123", "photo_credit": "TheSportsDB",
                     "photo_license": "第三方转载", "photo_match": "community_name_and_nationality", "fetched_at": now()}
        row = {"provider_id": "1", "name": "Kai Havertz", "nationality": "德国", "birth_date": "1999-06-11", **old_photo}
        squad = {"team_id": "arsenal", "roster": [row]}
        legacy_key = community_portraits.portrait_key(row, "arsenal", generation=1)
        self.store.set_meta(legacy_key, old_photo)
        replay = team_portraits.apply_cached_portraits(self.store, {"team_id": "arsenal", "roster": [dict(row)]})
        self.assertFalse(replay["roster"][0]["photo_url"])
        self.assertEqual(replay["roster"][0]["reason_code"], "identity_revalidation")
        with patch.object(wikimedia_portraits, "resolve_portrait", new=AsyncMock(return_value={"status": "missing"})), \
             patch.object(community_portraits, "resolve_portrait", new=AsyncMock(return_value={"status": "ambiguous"})) as fallback:
            result, _ = await team_portraits.enrich_player_photos(self.store, squad)
        self.assertEqual(fallback.await_count, 1)
        self.assertFalse(result["roster"][0]["photo_url"])
        self.assertTrue(squad["roster"][0]["photo_url"], "the caller's old snapshot remains untouched")
        self.assertEqual(self.store.get_meta(legacy_key), old_photo)

    async def test_force_rechecks_a_proven_community_photo(self):
        from tests.test_community_portraits import PLAYER, record, search
        with patch.object(community_portraits, "_search", new=AsyncMock(return_value=search(record("Kai Havertz", "Germany")))):
            photo = await community_portraits.resolve_portrait(self.store, PLAYER, "arsenal")
        squad = {"team_id": "arsenal", "roster": [{**PLAYER, **photo}]}
        with patch.object(wikimedia_portraits, "resolve_portrait", new=AsyncMock(return_value={"status": "missing"})), \
             patch.object(community_portraits, "resolve_portrait", new=AsyncMock(return_value={"status": "ambiguous"})) as fallback:
            result, _ = await team_portraits.enrich_player_photos(self.store, squad, player_ids=["1"], force=True)
        self.assertEqual(fallback.await_count, 1)
        self.assertFalse(result["roster"][0]["photo_url"])

    async def test_whole_team_retry_keeps_current_checked_community_and_wikimedia_caches(self):
        from tests.test_community_portraits import PLAYER, record, search
        with patch.object(community_portraits, "_search", new=AsyncMock(return_value=search(record("Kai Havertz", "Germany")))):
            community = await community_portraits.resolve_portrait(self.store, PLAYER, "arsenal")
        with patch.object(wikimedia_portraits, "request_json", side_effect=self.replies()):
            wiki = await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal")
        squad = {"team_id": "arsenal", "roster": [{**PLAYER, **community},
            {"provider_id": "2", **self.player, **wiki}]}
        with patch.object(wikimedia_portraits, "resolve_portrait", new=AsyncMock(side_effect=AssertionError("current photo"))) as primary, \
             patch.object(community_portraits, "resolve_portrait", new=AsyncMock(side_effect=AssertionError("current photo"))) as fallback:
            result, summary = await team_portraits.enrich_player_photos(self.store, squad, force=True)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual([row["photo_url"] for row in result["roster"]], [community["photo_url"], wiki["photo_url"]])
        primary.assert_not_awaited()
        fallback.assert_not_awaited()

    async def test_explicit_recheck_network_fault_keeps_current_community_and_wiki_photos(self):
        from tests.test_community_portraits import PLAYER, record, search
        with patch.object(community_portraits, "_search", new=AsyncMock(return_value=search(record("Kai Havertz", "Germany")))):
            community = await community_portraits.resolve_portrait(self.store, PLAYER, "arsenal")
        squad = {"team_id": "arsenal", "roster": [{**PLAYER, **community}]}
        with patch.object(wikimedia_portraits, "resolve_portrait", new=AsyncMock(return_value={"status": "missing"})), \
             patch.object(community_portraits, "_search", new=AsyncMock(side_effect=ConnectionError("offline"))):
            result, summary = await team_portraits.enrich_player_photos(self.store, squad, player_ids=["1"], force=True)
        self.assertEqual(result["roster"][0]["photo_url"], community["photo_url"])
        self.assertEqual(summary["status"], "partial")
        self.assertIn("TheSportsDB", summary["errors"][0])
        replay = team_portraits.apply_cached_portraits(self.store, result)
        self.assertEqual(replay["roster"][0]["photo_url"], community["photo_url"])
        with patch.object(wikimedia_portraits, "request_json", side_effect=self.replies()):
            wiki = await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal")
        squad = {"team_id": "arsenal", "roster": [{"provider_id": "2", **self.player, **wiki}]}
        with patch.object(wikimedia_portraits, "request_json", new=AsyncMock(side_effect=ConnectionError("offline"))):
            result, summary = await team_portraits.enrich_player_photos(self.store, squad, player_ids=["2"], force=True)
        self.assertEqual(result["roster"][0]["photo_url"], wiki["photo_url"])
        self.assertEqual(summary["status"], "partial")
        replay = team_portraits.apply_cached_portraits(self.store, result)
        self.assertEqual(replay["roster"][0]["photo_url"], wiki["photo_url"])

    async def test_cached_rejection_clears_old_wikimedia_photo_without_network(self):
        squad = {"team_id": "arsenal", "season_id": "eng.1:2026", "roster": [{
            "id": "espn-player-1", "provider_id": "1", "name": "Martin Ødegaard",
            "birth_date": "1998-12-17", "number": "8",
            "photo_url": "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/Martin.jpg/300px-Martin.jpg",
            "photo_match": "wikimedia_verified_club", "photo_license": "CC BY-SA 4.0",
            "photo_file_page": "https://commons.wikimedia.org/wiki/File:Martin.jpg"}]}
        player = squad["roster"][0]
        self.store.set_meta(wikimedia_portraits._portrait_key(player, "arsenal"), {
            "status": "rejected", "photo_reason": "页面记载的当前俱乐部与阵容不一致。", "fetched_at": now()})
        with self.community_silent(), \
                patch.object(wikimedia_portraits, "resolve_portrait", new_callable=AsyncMock) as resolve:
            result, summary = await team_portraits.enrich_player_photos(self.store, squad)
        resolve.assert_not_awaited()
        self.assertFalse(result["roster"][0]["photo_url"])
        self.assertFalse(result["roster"][0].get("photo_file_page"))
        self.assertIn("俱乐部", result["roster"][0]["photo_reason"])
        self.assertEqual(summary["status"], "missing")

    async def test_public_roster_reads_obey_current_verdicts_before_old_photos(self):
        with patch.object(wikimedia_portraits, "request_json", side_effect=self.replies()):
            photo = await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal")
        player = {"provider_id": "1", **self.player, **photo}
        current_key = wikimedia_portraits._portrait_key(player, "arsenal")
        self.store.set_meta(wikimedia_portraits._portrait_key(player, "arsenal", generation=1), photo)
        verdicts = [{"status": status, "fetched_at": now()} for status in ("rejected", "missing")]
        verdicts += [{**photo, flag: value} for flag, value in
                     (("private", True), ("deleted", True), ("restricted", True), ("missing", ""), ("suppressed", ""))]
        verdicts.append({"status": "rejected", "fetched_at": "2020-01-01T00:00:00Z"})
        for verdict in verdicts:
            with self.subTest(verdict=verdict):
                self.store.set_meta(current_key, verdict)
                shown = team_portraits.apply_cached_portraits(self.store,
                    {"team_id": "arsenal", "roster": [copy.deepcopy(player)]})["roster"][0]
                self.assertFalse(shown["photo_url"])
                self.assertFalse(any(shown.get(field) for field in
                    ("photo_source_url", "photo_file_page", "photo_credit", "photo_license", "photo_restrictions")))
        self.assertTrue(player["photo_url"], "stored facts and the caller snapshot remain unchanged")

    async def test_public_network_fallback_requires_a_fresh_complete_public_last_success(self):
        with patch.object(wikimedia_portraits, "request_json", side_effect=self.replies()):
            photo = await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal")
        player = {"provider_id": "1", **self.player, **photo}
        key = wikimedia_portraits._portrait_key(player, "arsenal")
        self.store.set_meta(key, {"status": "unavailable", "last_attempt": now(), "last_success": photo})
        shown = team_portraits.apply_cached_portraits(self.store,
            {"team_id": "arsenal", "roster": [copy.deepcopy(player)]})["roster"][0]
        self.assertEqual(shown["photo_url"], photo["photo_url"])
        self.assertEqual(shown["photo_updated_at"], photo["fetched_at"], "a fault must not renew original proof")
        for invalid in ({**photo, "private": True}, {**photo, "suppressed": ""},
                        {**photo, "photo_credit": ""}, {**photo, "photo_match": "unverified"},
                        {**photo, "fetched_at": "2020-01-01T00:00:00Z"}):
            with self.subTest(last_success=invalid):
                self.store.set_meta(key, {"status": "unavailable", "last_attempt": now(), "last_success": invalid})
                shown = team_portraits.apply_cached_portraits(self.store,
                    {"team_id": "arsenal", "roster": [copy.deepcopy(player)]})["roster"][0]
                self.assertFalse(shown["photo_url"])
                self.assertNotIn("photo_file_page", shown)

    async def test_current_commons_denial_overrides_portrait_and_network_last_success(self):
        with patch.object(wikimedia_portraits, "request_json", side_effect=self.replies()):
            photo = await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal")
        player = {"provider_id": "1", **self.player, **photo}
        self.store.set_meta(wikimedia_portraits._commons_key("Martin.jpg"),
                            {"status": "rejected", "fetched_at": now(), "photo": None})
        for current in (photo, {"status": "unavailable", "last_attempt": now(), "last_success": photo}):
            with self.subTest(current=current):
                self.store.set_meta(wikimedia_portraits._portrait_key(player, "arsenal"), current)
                for row in (copy.deepcopy(player), {"provider_id": "1", **self.player, "photo_url": ""}):
                    shown = team_portraits.apply_cached_portraits(self.store,
                        {"team_id": "arsenal", "roster": [row]})["roster"][0]
                    self.assertFalse(shown["photo_url"])
                    self.assertNotIn("photo_source_url", shown)
                    self.assertNotIn("photo_file_page", shown)

    async def test_selected_player_enrichment_cannot_return_another_players_revoked_photo(self):
        with patch.object(wikimedia_portraits, "request_json", side_effect=self.replies()):
            photo = await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal")
        revoked = {"provider_id": "1", **self.player, **photo}
        self.store.set_meta(wikimedia_portraits._portrait_key(revoked, "arsenal"),
                            {"status": "rejected", "fetched_at": now(), "photo_reason": "来源已撤回。"})
        current = {"provider_id": "2", "name": "Example Player", **team_portraits.espn_headshot({
            "id": "2", "headshot": {"href": "https://a.espncdn.com/i/headshots/soccer/players/full/2.png"}}, fetched_at=now())}
        result, summary = await team_portraits.enrich_player_photos(self.store,
            {"team_id": "arsenal", "roster": [revoked, current]}, player_ids=["2"])
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["available_count"], 1)
        self.assertFalse(result["roster"][0]["photo_url"])
        self.assertNotIn("photo_file_page", result["roster"][0])
        self.assertTrue(result["roster"][1]["photo_url"])
        self.assertTrue(revoked["photo_url"], "the input snapshot remains untouched")

    def test_commons_withdrawal_flags_reject_preserved_page_and_imageinfo_metadata(self):
        for layer in ("page", "imageinfo"):
            for flag, value in (("private", True), ("deleted", True), ("restricted", True),
                                ("missing", ""), ("suppressed", ""), ("filehidden", "")):
                with self.subTest(layer=layer, flag=flag):
                    payload = commons()
                    record = payload["query"]["pages"]["2"]
                    (record if layer == "page" else record["imageinfo"][0])[flag] = value
                    self.assertIsNone(wikimedia_portraits._license_metadata(payload, "Martin.jpg"))
        self.assertIsNotNone(wikimedia_portraits._license_metadata(commons(), "Martin.jpg"))

    async def test_cancelled_forced_revalidation_preserves_the_prior_source_verdict(self):
        with patch.object(wikimedia_portraits, "request_json", side_effect=self.replies()):
            photo = await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal")
        key = wikimedia_portraits._portrait_key(self.player, "arsenal")
        for prior in (photo, {"status": "rejected", "fetched_at": now(), "photo_reason": "来源已撤回。"}):
            with self.subTest(prior=prior):
                self.store.set_meta(key, prior)
                with patch.object(wikimedia_portraits, "request_json", new=AsyncMock(side_effect=asyncio.CancelledError)):
                    with self.assertRaises(asyncio.CancelledError):
                        await wikimedia_portraits.resolve_portrait(self.store, self.player, "arsenal", force=True)
                self.assertEqual(self.store.get_meta(key), prior)

    def test_current_club_ignores_parent_club_after_on_loan_from(self):
        self.assertEqual(wikimedia_portraits._current_club(
            "Player is a footballer who plays for Chelsea on loan from Arsenal.", "arsenal"), "other_club")
        self.assertEqual(wikimedia_portraits._current_club(
            "Player is a footballer who plays for Arsenal on loan from Chelsea.", "arsenal"), "confirmed")

    def test_current_club_ignores_former_team_after_leaving_clause(self):
        text = "Player is a footballer who plays for Chelsea after leaving Arsenal."
        self.assertEqual(wikimedia_portraits._current_club(text, "arsenal"), "other_club")
        self.assertEqual(wikimedia_portraits._current_club(text, "chelsea"), "confirmed")

    async def test_cancelled_reader_keeps_shared_host_slot_until_physical_request_finishes(self):
        release = threading.Event()
        started = []
        lock = threading.Lock()
        def slow_document(url, **kwargs):
            with lock:
                started.append(url)
            release.wait(2)
            return b'{"query":{"pages":{}}}', url, "application/json"
        async def wait_started(count):
            for _ in range(100):
                if len(started) >= count:
                    return
                await asyncio.sleep(0.01)
            self.fail(f"Only {len(started)} physical requests started")
        with patch.object(wikimedia_portraits, "fetch_document", side_effect=slow_document), \
             patch.object(wikimedia_portraits, "WIKIMEDIA_MIN_INTERVAL_SECONDS", 0):
            tasks = [asyncio.create_task(wikimedia_portraits.request_json(self.store, "en.wikipedia.org",
                     {"action": "query", "format": "json", "titles": f"Player {i}"})) for i in range(2)]
            try:
                await wait_started(2)
                tasks[0].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await tasks[0]
                third = asyncio.create_task(wikimedia_portraits.request_json(self.store, "en.wikipedia.org",
                    {"action": "query", "format": "json", "titles": "Player 3"}))
                tasks.append(third)
                await asyncio.sleep(0.05)
                self.assertEqual(len(started), 2)
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    def test_future_or_ended_wikidata_membership_is_not_current(self):
        def claim(start=None, end=None, qid="Q42"):
            qualifiers = {}
            if start:
                qualifiers["P580"] = [{"datavalue": {"value": {"time": start}}}]
            if end:
                qualifiers["P582"] = [{"datavalue": {"value": {"time": end}}}]
            return {"mainsnak": {"datavalue": {"value": {"id": qid}}}, "qualifiers": qualifiers}
        active, undated = wikimedia_portraits._team_claim_ids({"P54": [
            claim(start="+2099-01-01T00:00:00Z"), claim(end="+2020-01-01T00:00:00Z")]})
        self.assertEqual((active, undated), (set(), set()))

    def test_undated_career_claim_is_an_unproven_candidate_not_a_rejection(self):
        # Wikidata career entries often carry no P580/P582 at all. Discarding
        # them rejected real players whose only membership claims were undated.
        def claim(qid, start=None, end=None):
            qualifiers = {}
            if start:
                qualifiers["P580"] = [{"datavalue": {"value": {"time": start}}}]
            if end:
                qualifiers["P582"] = [{"datavalue": {"value": {"time": end}}}]
            return {"mainsnak": {"datavalue": {"value": {"id": qid}}}, "qualifiers": qualifiers}
        active, undated = wikimedia_portraits._team_claim_ids({"P54": [
            claim("Q9617"), claim("Q9617", end="+2020-01-01T00:00:00Z"), claim("Q99")]})
        self.assertEqual(active, set())
        self.assertEqual(undated, {"Q9617", "Q99"})

    def test_club_label_matches_despite_punctuation(self):
        # "Arsenal F.C." normalises to "arsenal f c" and never equalled the
        # stored alias "arsenal fc", so every real player was rejected.
        import server.club_library as club_library
        self.assertIn("arsenal fc", {alias.lower() for alias in club_library.TEAM_ALIASES["arsenal"]})
        normalised = wikimedia_portraits._name("Arsenal F.C.").replace(" ", "")
        self.assertEqual(normalised, "arsenal f c".replace(" ", ""))
        self.assertIn(normalised, {wikimedia_portraits._name(a).replace(" ", "")
                                   for a in club_library.TEAM_ALIASES["arsenal"]})
