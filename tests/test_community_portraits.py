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

    # --- the birth-date tiers -------------------------------------------------
    # A publisher that states the same birth date to the day has stated a fact
    # stronger than nationality, which dual nationals state inconsistently.

    async def resolve_with_surname(self, by_name, by_surname, player, **kwargs):
        """Route the two paced lookups the weak tier performs to their payloads."""
        calls = []

        async def route(_store, query):
            calls.append(query)
            return by_surname if query != player.get("name") else by_name

        with patch.object(community_portraits, "_search", new=AsyncMock(side_effect=route)):
            result = await community_portraits.resolve_portrait(self.store, player, "arsenal", **kwargs)
        return result, calls

    async def test_an_exact_birth_date_settles_a_country_the_roster_states_differently(self):
        # Dual nationals are the case this exists for: ESPN says Morocco and the
        # publisher says France, while both state the same day of birth.
        player = {**PLAYER, "name": "Yassin Belkhdim", "nationality": "摩洛哥", "birth_date": "2002-02-14"}
        result, _ = await self.resolve(search(record("Yassin Belkhdim", "France", dateBorn="2002-02-14")),
                                       player=player)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["photo_match"], "community_name_and_birthdate")

    async def test_a_shared_birth_year_separates_the_publishers_imprecision_from_a_namesake(self):
        player = {**PLAYER, "birth_date": "1997-10-23"}
        same_year, _ = await self.resolve(search(record("Kai Havertz", "Germany", dateBorn="1997-06-06")),
                                          player=player)
        self.assertEqual(same_year["status"], "completed")
        self.assertEqual(same_year["photo_match"], "community_name_and_birth_year")
        # A different year is a different person wearing the same name.
        other_year, _ = await self.resolve(search(record("Kai Havertz", "Germany", dateBorn="1987-12-29")),
                                           player=player, force=True)
        self.assertEqual(other_year["status"], "ambiguous")
        self.assertFalse(other_year.get("photo_url"))

    async def test_a_surname_spelling_variant_is_accepted_only_on_an_exact_birth_date(self):
        # The publisher spells the player "Branco Boomen" for "Branco van den
        # Boomen". The surname search reaches it; only the birth date may accept
        # it, and a country disagreement must not block that.
        player = {**PLAYER, "name": "Branco van den Boomen", "nationality": "荷兰",
                  "birth_date": "1995-07-21"}
        found, calls = await self.resolve_with_surname(
            search(), search(record("Branco Boomen", "The Netherlands", dateBorn="1995-07-21")), player)
        self.assertEqual(found["status"], "completed")
        self.assertEqual(found["photo_match"], "community_surname_and_birthdate")
        # The second request is spent on the surname alone, in the folded form
        # the comparison itself uses, not the roster's full spelling.
        self.assertEqual(calls, ["Branco van den Boomen", "boomen"])

    async def test_a_surname_match_without_an_exact_birth_date_publishes_nothing(self):
        player = {**PLAYER, "name": "Branco van den Boomen", "nationality": "荷兰",
                  "birth_date": "1995-07-21"}
        # Same surname, but only the year agrees: not enough for a partial name.
        result, _ = await self.resolve_with_surname(
            search(), search(record("Branco Boomen", "The Netherlands", dateBorn="1995-01-01")), player)
        self.assertEqual(result["status"], "missing")
        self.assertFalse(result.get("photo_url"))

    async def test_a_short_surname_is_never_searched(self):
        # "Lee" would return a hundred strangers and no date could confirm one.
        player = {**PLAYER, "name": "Ji Lee", "nationality": "韩国", "birth_date": "2001-03-04"}
        result, calls = await self.resolve_with_surname(search(), search(), player)
        self.assertEqual(calls, ["Ji Lee"])
        self.assertEqual(result["status"], "missing")

    async def test_a_name_alone_still_publishes_nothing(self):
        # No country and no birthday leaves one identity fact, so no request is
        # spent on a lookup that could never be accepted.
        result, search_call = await self.resolve(search(record("Kai Havertz", "Germany")),
                                                 player={**PLAYER, "nationality": "", "birth_date": ""})
        self.assertEqual(result["status"], "ambiguous")
        self.assertFalse(result.get("photo_url"))
        self.assertEqual(search_call.await_count, 0)

    async def test_a_birthdate_based_match_stays_valid_and_a_contradiction_does_not(self):
        # The saved verdict is re-derived, not trusted: the same evidence is
        # accepted, and a roster birth date that now contradicts it is not.
        player = {**PLAYER, "birth_date": "1999-06-11"}
        verified, _ = await self.resolve(search(record("Kai Havertz", "Germany", dateBorn="1999-06-11")), player=player)
        self.assertEqual(verified["photo_match"], "community_name_and_birthdate")
        self.assertTrue(community_portraits.cached_portrait(self.store, player, "arsenal"))
        changed = {**player, "birth_date": "1990-01-01"}
        self.assertIsNone(community_portraits.cached_portrait(self.store, changed, "arsenal"))

    async def test_a_surname_match_survives_its_own_cache_check(self):
        # The weak tier exists because the two spellings differ, so the saved
        # completion must not be rejected for the very difference that allowed
        # it to be made — that would drop a fresh photo on the next pass.
        player = {**PLAYER, "name": "Branco van den Boomen", "nationality": "荷兰",
                  "birth_date": "1995-07-21"}
        found, _ = await self.resolve_with_surname(
            search(), search(record("Branco Boomen", "The Netherlands", dateBorn="1995-07-21")), player)
        self.assertEqual(found["photo_match"], "community_surname_and_birthdate")
        self.assertTrue(community_portraits.cached_portrait(self.store, player, "arsenal"))
        # A different surname is a different person and must not be reused.
        stranger = {**player, "name": "Branco Anders"}
        self.assertIsNone(community_portraits.cached_portrait(self.store, stranger, "arsenal"))

    async def test_a_refresh_can_reach_every_community_match_the_new_rules_produce(self):
        from server import team_portraits
        self.assertTrue(community_portraits.COMMUNITY_PHOTO_MATCHES <= team_portraits.REFRESHABLE_PHOTO_MATCHES)
        for match in community_portraits.COMMUNITY_PHOTO_MATCHES:
            self.assertEqual(team_portraits.photo_source_rank(match),
                             team_portraits.photo_source_rank("community_name_and_nationality"))
