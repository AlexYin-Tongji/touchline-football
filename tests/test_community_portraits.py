"""Community portraits must never publish an image on one identity fact alone."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server import community_portraits, ingestion
from server.storage import Store


PLAYER = {"provider_id": "1", "name": "Kai Havertz", "number": "29", "nationality": "德国",
          "birth_date": "", "photo_url": ""}
CUTOUT = "https://r2.thesportsdb.com/images/media/player/cutout/abc123.png"


def search(*records):
    return {"player": list(records)}


def record(name, nationality, **extra):
    return {"idPlayer": "34169226", "strPlayer": name, "strNationality": nationality,
            "strSport": "Soccer", "strStatus": "Active", "strCutout": CUTOUT, **extra}


class CommunityPortraitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "portraits.sqlite3")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def resolve(self, payload, player=None, **kwargs):
        with patch.object(community_portraits, "_search", new=AsyncMock(return_value=payload)) as search_call:
            result = await community_portraits.resolve_portrait(self.store, player or PLAYER, "arsenal", **kwargs)
        return result, search_call

    async def test_name_and_nationality_must_agree_before_a_photo_is_used(self):
        result, _ = await self.resolve(search(record("Kai Havertz", "Germany")))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["photo_url"], CUTOUT)
        self.assertEqual(result["photo_match"], "community_name_and_nationality")
        # An unlicensed community image must still carry its attribution and a
        # licence statement, or the reader surface refuses to render it.
        self.assertTrue(result["photo_credit"])
        self.assertTrue(result["photo_license"])
        self.assertTrue(result["photo_source_url"])

    async def test_a_different_club_in_the_publisher_record_is_not_evidence(self):
        # The publisher's club field was observed naming another club for a
        # player the roster lists, so it decides nothing in either direction.
        result, _ = await self.resolve(search(record("Kai Havertz", "Germany", strTeam="Chelsea")))
        self.assertEqual(result["status"], "completed")

    async def test_disagreeing_nationality_is_rejected(self):
        result, _ = await self.resolve(search(record("Kai Havertz", "France")))
        self.assertEqual(result["status"], "ambiguous")
        self.assertFalse(result.get("photo_url"))

    async def test_birth_date_contradiction_and_invalid_publisher_id_publish_nothing(self):
        for extra in ({"dateBorn": "2000-01-01"}, {"idPlayer": "../other-player"}):
            with self.subTest(extra=extra):
                result, _ = await self.resolve(search(record("Kai Havertz", "Germany", **extra)),
                    player={**PLAYER, "birth_date": "1999-06-11"}, force=True)
                self.assertEqual(result["status"], "ambiguous")
                self.assertFalse(result.get("photo_url"))

    async def test_source_spellings_for_us_nationality_and_non_ascii_names_match(self):
        result, _ = await self.resolve(search(record("Martin Odegaard", "Norway")),
            player={**PLAYER, "name": "Martin Ødegaard", "nationality": "挪威"})
        self.assertEqual(result["status"], "completed")
        result, _ = await self.resolve(search(record("Gaga Slonina", "United States")),
            player={**PLAYER, "name": "Gaga Slonina", "nationality": "USA"})
        self.assertEqual(result["status"], "completed")

    async def test_changed_roster_nationality_cannot_reuse_a_checked_photo(self):
        first, _ = await self.resolve(search(record("Kai Havertz", "Germany")))
        self.assertEqual(first["status"], "completed")
        second, call = await self.resolve(search(record("Kai Havertz", "Germany")),
                                          player={**PLAYER, "nationality": "法国"})
        self.assertEqual(second["status"], "ambiguous")
        self.assertEqual(call.await_count, 1)

    async def test_previous_generation_requires_saved_identity_evidence_and_stays_read_only(self):
        verified, _ = await self.resolve(search(record("Kai Havertz", "Germany", dateBorn="1999-06-11")),
                                          player={**PLAYER, "birth_date": "1999-06-11"})
        player = {**PLAYER, "birth_date": "1999-06-11"}
        key = community_portraits.portrait_key(player, "arsenal")
        legacy_key = community_portraits.portrait_key(player, "arsenal", generation=1)
        self.store.set_meta(key, {})
        self.store.set_meta(legacy_key, verified)
        self.assertEqual(community_portraits.cached_portrait(self.store, player, "arsenal"), verified)
        self.assertEqual(self.store.get_meta(legacy_key), verified)
        old = {key: value for key, value in verified.items() if key != "_identity"}
        self.store.set_meta(legacy_key, old)
        self.assertIsNone(community_portraits.cached_portrait(self.store, player, "arsenal"))
        updated, call = await self.resolve(search(record("Kai Havertz", "Germany", dateBorn="1999-06-11")),
                                           player=player)
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(call.await_count, 1)
        self.assertTrue(updated["_identity"])
        self.assertEqual(self.store.get_meta(legacy_key), old)

    async def test_cached_completion_rechecks_saved_birthday_and_source_identity(self):
        player = {**PLAYER, "birth_date": "1999-06-11"}
        verified, _ = await self.resolve(search(record("Kai Havertz", "Germany", dateBorn="1999-06-11")), player=player)
        for extra in ({"_identity": {**verified["_identity"], "birth_date": "2000-01-01"}},
                      {"photo_source_url": "https://www.thesportsdb.com/player/other"},
                      {"photo_url": "https://evil.example/portrait.png"}):
            with self.subTest(extra=extra):
                self.store.set_meta(community_portraits.portrait_key(player, "arsenal"), {**verified, **extra})
                self.assertIsNone(community_portraits.cached_portrait(self.store, player, "arsenal"))

    async def test_network_failure_preserves_only_current_identity_proof_and_rejection_replaces_it(self):
        verified, _ = await self.resolve(search(record("Kai Havertz", "Germany")))
        with patch.object(community_portraits, "_search", new=AsyncMock(side_effect=ConnectionError("offline"))):
            fault = await community_portraits.resolve_portrait(self.store, PLAYER, "arsenal", force=True)
        self.assertEqual(fault["status"], "unavailable")
        self.assertEqual(community_portraits.cached_portrait(self.store, PLAYER, "arsenal")["photo_url"], verified["photo_url"])
        from server import team_portraits
        view = team_portraits.apply_cached_portraits(self.store, {"team_id": "arsenal", "roster": [{**PLAYER, **verified}]})
        self.assertEqual(view["roster"][0]["photo_url"], verified["photo_url"])
        # A newer contradictory identity is a verdict, so it must replace the
        # old completion and must not retain a last-success fallback.
        rejected, _ = await self.resolve(search(record("Kai Havertz", "France")), force=True)
        self.assertEqual(rejected["status"], "ambiguous")
        self.assertNotIn("last_success", rejected)
        view = team_portraits.apply_cached_portraits(self.store, {"team_id": "arsenal", "roster": [{**PLAYER, **verified}]})
        self.assertFalse(view["roster"][0]["photo_url"])

    async def test_namesakes_are_separated_only_by_nationality(self):
        result, _ = await self.resolve(search(record("Kai Havertz", "France"), record("Kai Havertz", "Germany")))
        self.assertEqual(result["status"], "completed")
        # Two namesakes of which the roster's country is one: the roster entry
        # still resolves to exactly one record, so this is accepted.
        self.assertEqual(result["photo_url"], CUTOUT)

    async def test_namesakes_without_a_matching_country_publish_nothing(self):
        # Neither namesake is the roster's country, so nothing can separate
        # them and no face may be chosen on the strength of a shared name.
        both_foreign, search_call = await self.resolve(
            search(record("Kai Havertz", "France"), record("Kai Havertz", "Turkey")),
            player={**PLAYER, "number": "7"})
        self.assertEqual(both_foreign["status"], "ambiguous")
        self.assertFalse(both_foreign.get("photo_url"))
        self.assertTrue(search_call.await_count)

    async def test_missing_roster_nationality_asks_no_question(self):
        result, search_call = await self.resolve(search(record("Kai Havertz", "Germany")),
                                                 player={**PLAYER, "nationality": ""})
        self.assertEqual(result["status"], "ambiguous")
        self.assertFalse(result.get("photo_url"))
        self.assertEqual(search_call.await_count, 0)

    async def test_the_publishers_own_country_spelling_still_matches(self):
        result, _ = await self.resolve(search(record("Kai Havertz", "The Netherlands")),
                                       player={**PLAYER, "nationality": "荷兰"})
        self.assertEqual(result["status"], "completed")

    async def test_a_throttle_and_a_transport_fault_stay_retryable(self):
        for error in (ingestion.IngestionError("来源返回 HTTP 429，请求过于频繁，请稍后重试。"),
                      ingestion.SourceConnectionError("来源请求超时，请稍后重试")):
            with self.subTest(error=str(error)):
                self.store.set_meta(community_portraits.portrait_key(PLAYER, "arsenal"), {})
                with patch.object(community_portraits, "_search", new=AsyncMock(side_effect=error)):
                    result = await community_portraits.resolve_portrait(self.store, PLAYER, "arsenal")
                # A failure to find out must not become a verdict about the
                # player, which is what froze whole squads at zero photos.
                self.assertEqual(result["status"], "unavailable")
                self.assertTrue(result["retryable"])
                self.assertTrue(result["reason_code"])

    async def test_only_the_publishers_own_https_image_paths_are_accepted(self):
        for bad in ("http://r2.thesportsdb.com/images/media/player/cutout/a.png",
                    "https://evil.example/images/media/player/cutout/a.png",
                    "https://r2.thesportsdb.com/logo.png",
                    "https://r2.thesportsdb.com/images/media/team/cutout/a.png",
                    "javascript:alert(1)"):
            with self.subTest(url=bad):
                self.assertEqual(community_portraits._safe_image_url(bad), "")
        self.assertEqual(community_portraits._safe_image_url(CUTOUT), CUTOUT)

    async def test_a_cached_verdict_is_reused_without_another_request(self):
        first, _ = await self.resolve(search(record("Kai Havertz", "Germany")))
        self.assertEqual(first["status"], "completed")
        with patch.object(community_portraits, "_search", new=AsyncMock(side_effect=AssertionError("network"))) as again:
            second = await community_portraits.resolve_portrait(self.store, PLAYER, "arsenal")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(again.await_count, 0)

    async def test_an_unsafe_image_url_yields_no_photo(self):
        result, _ = await self.resolve(search(record("Kai Havertz", "Germany",
                                                     strCutout="https://evil.example/p.png",
                                                     strThumb="https://evil.example/t.png")))
        self.assertEqual(result["status"], "missing")
        self.assertFalse(result.get("photo_url"))
