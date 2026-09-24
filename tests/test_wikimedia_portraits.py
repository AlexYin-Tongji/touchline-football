"""A free image is usable only after person, current club and file checks."""
import asyncio
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

    def replies(self, articles=None, license_result=None):
        articles = articles if articles is not None else [page()]
        license_result = license_result if license_result is not None else commons()
        async def answer(store, host, params):
            if params.get("list") == "search":
                return {"query": {"search": [{"title": a["query"]["pages"]["1"]["title"]} for a in articles]}}
            if host == "commons.wikimedia.org":
                return license_result
            title = params.get("titles")
            return next((a for a in articles if a["query"]["pages"]["1"]["title"] == title), {})
        return answer

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
        self.assertEqual(fetch.call_count, 3)

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
                self.assertLessEqual(fetch.call_count, 2)

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
        self.assertEqual(fetch.call_count, 3)

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
        with patch.object(wikimedia_portraits, "resolve_portrait", new_callable=AsyncMock,
                          return_value={"status": "rejected", "photo_reason": "页面记载的当前俱乐部与阵容不一致。"}):
            result, summary = await team_portraits.enrich_player_photos(self.store, squad, force=True)
        self.assertFalse(result["roster"][0]["photo_url"])
        self.assertFalse(result["roster"][0].get("photo_file_page"))
        self.assertEqual(summary["status"], "missing")

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
        with patch.object(wikimedia_portraits, "resolve_portrait", new_callable=AsyncMock) as resolve:
            result, summary = await team_portraits.enrich_player_photos(self.store, squad)
        resolve.assert_not_awaited()
        self.assertFalse(result["roster"][0]["photo_url"])
        self.assertFalse(result["roster"][0].get("photo_file_page"))
        self.assertIn("俱乐部", result["roster"][0]["photo_reason"])
        self.assertEqual(summary["status"], "missing")

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
