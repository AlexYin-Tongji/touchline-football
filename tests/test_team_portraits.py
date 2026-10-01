"""Official portraits require unique full-name and shirt-number identity."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server import community_portraits, team_portraits, wikimedia_portraits
from server.ingestion import now
from server.storage import Store


URL = team_portraits.DIRECTORIES["barcelona"]["url"]
PHOTO = "https://www.fcbarcelona.com/photo-resources/2026/07/player.jpg?width=470&height=470"


def card(first="Joan", last="García", number="1", image=PHOTO, path="110491/joan-garcia"):
    return f'<a class="team-person" href="{URL}/{path}"><span class="team-person__first-name">{first}</span><span class="team-person__last-name">{last}</span><span class="team-person__number">{number}</span><img class="team-person__image" src="" data-image-src="{image}"></a>'


def squad(name="Joan Garcia", number="1"):
    return {"team_id": "barcelona", "roster": [{"provider_id": "123", "name": name, "number": number, "photo_url": ""}]}


class PortraitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "portraits.sqlite3")
        for module in (wikimedia_portraits, community_portraits):
            offline = patch.object(module, "resolve_portrait", new=AsyncMock(return_value={"status": "missing"}))
            offline.start()
            self.addCleanup(offline.stop)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def fetch(self, html):
        return patch.object(team_portraits, "fetch_document", return_value=(html.encode(), URL, "text/html"))

    async def test_exact_accent_normalized_full_name_and_number_add_real_card_url(self):
        with self.fetch(card()) as fetch:
            result = await team_portraits.enrich_portraits(self.store, squad())
        player = result["roster"][0]
        self.assertEqual(player["photo_url"], PHOTO)
        self.assertEqual(player["photo_match"], "exact_name_and_number")
        self.assertIn("官方", player["photo_credit"])
        self.assertEqual(player["photo_source_url"], URL + "/110491/joan-garcia")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs["allowed_hosts"], {"www.fcbarcelona.com"})

    async def test_similar_surname_wrong_number_and_duplicate_card_never_match(self):
        for roster, html in ((squad("Eric Garcia"), card()), (squad(number="2"), card()), (squad(), card() + card())):
            self.store.set_meta("team_portraits_v1_barcelona", {})
            with self.fetch(html):
                result = await team_portraits.enrich_portraits(self.store, roster)
            self.assertEqual(result["roster"][0]["photo_url"], "")

    async def test_other_teams_and_existing_espn_photo_never_fetch_directory(self):
        other = squad()
        other["team_id"] = "arsenal"
        existing = squad()
        existing["roster"][0]["photo_url"] = "https://a.espncdn.com/real.png"
        with self.fetch(card()) as fetch:
            self.assertEqual(await team_portraits.enrich_portraits(self.store, other), other)
            self.assertEqual(await team_portraits.enrich_portraits(self.store, existing), existing)
        fetch.assert_not_called()

    async def test_same_source_portrait_is_retained_without_cross_source_lookup(self):
        for team_id in ("barcelona", "manchester-united", "mainz"):
            with self.subTest(team_id=team_id):
                snapshot = squad(name="Unknown Source Player")
                snapshot["team_id"] = team_id
                player = snapshot["roster"][0]
                player.update(team_portraits.espn_headshot({"id": "123", "headshot": {
                    "href": "https://a.espncdn.com/i/headshots/soccer/players/full/123.png"}}, fetched_at="2026-10-01T00:00:00Z"))
                with self.fetch(card()) as directory, \
                     patch.object(wikimedia_portraits, "resolve_portrait", AsyncMock()) as wiki, \
                     patch.object(community_portraits, "resolve_portrait", AsyncMock()) as community:
                    result, summary = await team_portraits.enrich_player_photos(self.store, snapshot, player_ids=["123"], force=True)
                self.assertEqual(result["roster"][0], player)
                self.assertEqual(summary["status"], "completed")
                self.assertEqual(summary["updated_count"], 0)
                directory.assert_not_called()
                wiki.assert_not_called()
                community.assert_not_called()

    async def test_cached_source_photo_cannot_follow_a_different_player_id(self):
        snapshot = squad()
        snapshot["team_id"] = "mainz"
        player = snapshot["roster"][0]
        player.update(team_portraits.espn_headshot({"id": "123", "headshot": {
            "href": "https://a.espncdn.com/i/headshots/soccer/players/full/123.png"}}, fetched_at="2026-10-01T00:00:00Z"))
        player["provider_id"] = "456"
        result = team_portraits.apply_cached_portraits(self.store, snapshot)
        self.assertFalse(result["roster"][0]["photo_url"])
        self.assertNotIn("photo_source_url", result["roster"][0])

    async def test_any_team_uses_four_identity_bound_source_pages_before_search(self):
        snapshot = {"team_id": "unlisted-local-club", "provider": "ESPN", "roster": [
            {"provider_id": str(index), "name": "Same Name", "position_group": group, "photo_url": ""}
            for index, group in enumerate(("GK", "DF", "MF", "FW"), start=1)]}
        def records(identity):
            return [{"id": identity, "name": "Same Name", "headshot": {
                "href": f"https://a.espncdn.com/i/headshots/soccer/players/full/{identity}.png"}}]
        with patch("server.espn_pages.fetch_player_portrait_records", new=AsyncMock(side_effect=records)) as source, \
             patch.object(wikimedia_portraits, "resolve_portrait", new=AsyncMock()) as wiki, \
             patch.object(community_portraits, "resolve_portrait", new=AsyncMock()) as community:
            result, summary = await team_portraits.enrich_player_photos(self.store, snapshot)
        self.assertEqual(source.await_count, 4)
        self.assertEqual(summary["available_count"], 4)
        self.assertEqual(summary["espn_attempted_count"], 4)
        for player in result["roster"]:
            self.assertIn("/" + player["provider_id"] + ".png", player["photo_url"])
            self.assertEqual(player["name"], "Same Name")
            self.assertEqual(player["photo_match"], "espn_directory_provider_id")
            self.assertTrue(player["photo_file_page"])
        wiki.assert_not_called()
        community.assert_not_called()
        with patch("server.espn_pages.fetch_player_portrait_records", new=AsyncMock()) as cached_source:
            repeated, _ = await team_portraits.enrich_player_photos(self.store, snapshot)
        cached_source.assert_not_called()
        self.assertEqual(repeated["roster"], result["roster"])

    async def test_removed_directory_headshot_cannot_survive_from_cache(self):
        snapshot = {"team_id": "mainz", "provider": "ESPN", "roster": [
            {"provider_id": "123", "name": "Example", "photo_url": ""}]}
        record = {"id": "123", "headshot": {"href": "https://a.espncdn.com/i/headshots/soccer/players/full/123.png"}}
        with patch("server.espn_pages.fetch_player_portrait_records", new=AsyncMock(return_value=[record])):
            first, _ = await team_portraits.enrich_player_photos(self.store, snapshot)
        self.assertTrue(first["roster"][0]["photo_url"])
        with patch("server.espn_pages.fetch_player_portrait_records", new=AsyncMock(return_value=[{"id": "123", "headshot": {}}])):
            withdrawn, _ = await team_portraits.enrich_player_photos(self.store, first, force=True)
        self.assertFalse(withdrawn["roster"][0]["photo_url"])
        self.assertNotIn("photo_file_page", withdrawn["roster"][0])

    def test_current_private_source_marker_blocks_its_previous_directory_photo(self):
        snapshot = {"team_id": "mainz", "provider": "ESPN", "roster": [
            {"provider_id": "123", "name": "Example", "photo_url": "", "photo_blocked_sources": ["espn"]}]}
        photo = team_portraits.espn_headshot({"id": "123", "headshot": {
            "href": "https://a.espncdn.com/i/headshots/soccer/players/full/123.png"}}, fetched_at="2026-10-01T00:00:00Z")
        self.store.set_meta("espn_portraits_v1_mainz", {"photos": {"123": {
            **photo, "photo_match": "espn_directory_provider_id"}}, "covered_ids": ["123"]})
        result = team_portraits.apply_cached_portraits(self.store, snapshot)
        self.assertFalse(result["roster"][0]["photo_url"])
        self.assertNotIn("photo_source_url", result["roster"][0])

    async def test_selector_revocation_blocks_main_photo_public_reads_and_rebuilt_rosters(self):
        photo = team_portraits.espn_headshot({"id": "123", "headshot": {
            "href": "https://a.espncdn.com/i/headshots/soccer/players/full/123.png"}}, fetched_at=now())
        other = {"id": "456", "headshot": {
            "href": "https://a.espncdn.com/i/headshots/soccer/players/full/456.png"}}
        for flag in ("private", "deleted", "restricted"):
            with self.subTest(flag=flag):
                self.store.set_meta("espn_portraits_v1_mainz", {})
                original = {"provider_id": "123", "name": "Example One", "position_group": "GK", **photo}
                snapshot = {"team_id": "mainz", "provider": "ESPN", "roster": [original,
                    {"provider_id": "456", "name": "Example Two", "position_group": "DF", "photo_url": ""}]}
                revoked = {"id": "123", flag: True, "headshot": {"href": photo["photo_url"]}}
                with patch("server.espn_pages.fetch_player_portrait_records", new=AsyncMock(return_value=[revoked, other])), \
                     patch.object(wikimedia_portraits, "resolve_portrait", new=AsyncMock(return_value={"status": "missing"})), \
                     patch.object(community_portraits, "resolve_portrait", new=AsyncMock(return_value={"status": "missing"})):
                    result, _ = await team_portraits.enrich_player_photos(self.store, snapshot, player_ids=["456"], force=True)
                directory = self.store.get_meta("espn_portraits_v1_mainz")
                self.assertEqual(directory["revoked_ids"], ["123"])
                self.assertNotIn("123", directory["photos"])
                self.assertTrue(result["roster"][1]["photo_url"], "covered positive IDs are not revoked")
                for replay in (result, {"team_id": "mainz", "roster": [copy.deepcopy(original)]}):
                    shown = team_portraits.apply_cached_portraits(self.store, replay)["roster"][0]
                    self.assertFalse(shown["photo_url"])
                    self.assertFalse(any(shown.get(key) for key in
                        ("photo_source_url", "photo_file_page", "photo_license", "photo_restrictions")))
                self.assertTrue(original["photo_url"], "input facts remain unchanged")

    async def test_selector_tombstone_survives_failures_until_checked_public_replacement(self):
        photo = team_portraits.espn_headshot({"id": "123", "headshot": {
            "href": "https://a.espncdn.com/i/headshots/soccer/players/full/123.png"}}, fetched_at=now())
        self.store.set_meta("espn_portraits_v1_mainz", {"revoked_ids": ["123"], "covered_ids": ["123"],
            "photos": {"123": {**photo, "photo_match": "espn_directory_provider_id"}}})
        squad = {"team_id": "mainz", "provider": "ESPN", "roster": [{"provider_id": "123", "name": "Example", **photo}]}
        with patch("server.espn_pages.fetch_player_portrait_records", new=AsyncMock(side_effect=ConnectionError("offline"))):
            await team_portraits._enrich_espn_directory(self.store, squad, squad["roster"], force=True)
        self.assertFalse(squad["roster"][0]["photo_url"])
        self.assertEqual(self.store.get_meta("espn_portraits_v1_mainz")["revoked_ids"], ["123"])
        record = {"id": "123", "headshot": {"href": photo["photo_url"]}}
        with patch("server.espn_pages.fetch_player_portrait_records", new=AsyncMock(return_value=[record])):
            await team_portraits._enrich_espn_directory(self.store, squad, squad["roster"], force=True)
        self.assertEqual(self.store.get_meta("espn_portraits_v1_mainz")["revoked_ids"], [])
        self.assertEqual(squad["roster"][0]["photo_url"], photo["photo_url"])

    async def test_selector_refusal_wins_over_another_pages_duplicate_public_record(self):
        snapshot = {"team_id": "mainz", "provider": "ESPN", "roster": [
            {"provider_id": "123", "name": "Example One", "position_group": "GK", "photo_url": ""},
            {"provider_id": "456", "name": "Example Two", "position_group": "DF", "photo_url": ""}]}
        valid = {"id": "123", "headshot": {"href": "https://a.espncdn.com/i/headshots/soccer/players/full/123.png"}}
        with patch("server.espn_pages.fetch_player_portrait_records", new=AsyncMock(side_effect=[
                [{"id": "123", "private": True, "headshot": {}}], [valid]])):
            await team_portraits._enrich_espn_directory(self.store, snapshot, snapshot["roster"], force=True)
        self.assertFalse(snapshot["roster"][0]["photo_url"])
        self.assertEqual(self.store.get_meta("espn_portraits_v1_mainz")["revoked_ids"], ["123"])

    async def test_global_athlete_revocation_blocks_other_clubs_until_source_republishes(self):
        photo = team_portraits.espn_headshot({"id": "123", "headshot": {
            "href": "https://a.espncdn.com/i/headshots/soccer/players/full/123.png"}}, fetched_at=now())
        first = {"team_id": "mainz", "provider": "ESPN", "roster": [
            {"provider_id": "123", "name": "Example", "photo_url": ""}]}
        second = {"team_id": "bologna", "provider": "ESPN", "roster": [
            {"provider_id": "123", "name": "Example", **photo}]}
        directory = {"photos": {"123": {**photo, "photo_match": "espn_directory_provider_id"}}, "covered_ids": ["123"]}
        self.store.set_meta("espn_portraits_v1_bologna", directory)
        with patch("server.espn_pages.fetch_player_portrait_records", new=AsyncMock(return_value=[
                {"id": "123", "deleted": True, "headshot": {}}])):
            await team_portraits._enrich_espn_directory(self.store, first, first["roster"], force=True)
        global_before = self.store.get_meta("espn_portrait_visibility_v1")
        for match in ("espn_provider_id", "espn_directory_provider_id"):
            roster = copy.deepcopy(second)
            roster["roster"][0]["photo_match"] = match
            shown = team_portraits.apply_cached_portraits(self.store, roster)["roster"][0]
            self.assertFalse(shown["photo_url"])
            self.assertNotIn("photo_source_url", shown)
        self.assertEqual(self.store.get_meta("espn_portraits_v1_bologna"), directory, "public reads do not write caches")
        self.assertEqual(self.store.get_meta("espn_portrait_visibility_v1"), global_before)
        self.assertTrue(second["roster"][0]["photo_url"], "stored squad remains untouched")
        with patch("server.espn_pages.fetch_player_portrait_records", new=AsyncMock(side_effect=ConnectionError("offline"))):
            await team_portraits._enrich_espn_directory(self.store, second, second["roster"], force=True)
        self.assertEqual(self.store.get_meta("espn_portrait_visibility_v1")["records"]["123"]["status"], "withdrawn")
        with patch("server.espn_pages.fetch_player_portrait_records", new=AsyncMock(return_value=[
                {"id": "123", "headshot": {"href": photo["photo_url"]}}])):
            await team_portraits._enrich_espn_directory(self.store, second, second["roster"], force=True)
        self.assertEqual(self.store.get_meta("espn_portrait_visibility_v1")["records"]["123"]["status"], "public")
        rebuilt = {"team_id": "mainz", "provider": "ESPN", "roster": [
            {"provider_id": "123", "name": "Example", **photo}]}
        shown = team_portraits.apply_cached_portraits(self.store, rebuilt)["roster"][0]
        self.assertEqual(shown["photo_url"], photo["photo_url"], "a new checked public record releases old team tombstones")

    async def test_older_concurrent_selector_success_cannot_undo_newer_global_revocation(self):
        started, release = asyncio.Event(), asyncio.Event()
        first = {"team_id": "mainz", "provider": "ESPN", "roster": [
            {"provider_id": "123", "name": "Example", "photo_url": ""}]}
        second = {"team_id": "bologna", "provider": "ESPN", "roster": copy.deepcopy(first["roster"])}
        async def older_success(identity):
            started.set()
            await release.wait()
            return [{"id": identity, "headshot": {
                "href": "https://a.espncdn.com/i/headshots/soccer/players/full/123.png"}}]
        with patch("server.espn_pages.fetch_player_portrait_records", new=AsyncMock(side_effect=older_success)):
            older = asyncio.create_task(team_portraits._enrich_espn_directory(self.store, first, first["roster"], force=True))
            await started.wait()
            try:
                with patch("server.espn_pages.fetch_player_portrait_records", new=AsyncMock(return_value=[
                        {"id": "123", "private": True, "headshot": {}}])):
                    await team_portraits._enrich_espn_directory(self.store, second, second["roster"], force=True)
            finally:
                release.set()
                await older
        self.assertEqual(self.store.get_meta("espn_portrait_visibility_v1")["records"]["123"]["status"], "withdrawn")
        self.assertFalse(first["roster"][0]["photo_url"])

    async def test_concurrent_refreshes_share_one_bounded_directory_fetch(self):
        with self.fetch(card()) as fetch:
            one, two = await asyncio.gather(team_portraits.enrich_portraits(self.store, squad()), team_portraits.enrich_portraits(self.store, squad()))
        self.assertEqual(one["roster"][0]["photo_url"], PHOTO)
        self.assertEqual(two["roster"][0]["photo_url"], PHOTO)
        self.assertEqual(fetch.call_count, 1)

    async def test_failure_preserves_prior_checked_photo_and_original_timestamp(self):
        saved = {"team_id": "barcelona", "cards": team_portraits.parse_directory(card(), "barcelona"),
                 "fetched_at": "2020-01-01T00:00:00+00:00", "last_attempt": "2020-01-01T00:00:00+00:00"}
        self.store.set_meta("team_portraits_v1_barcelona", saved)
        with patch.object(team_portraits, "fetch_document", side_effect=ValueError("unavailable")) as fetch:
            result = await team_portraits.enrich_portraits(self.store, squad())
            await team_portraits.enrich_portraits(self.store, squad())
        self.assertEqual(result["roster"][0]["photo_url"], PHOTO)
        self.assertEqual(result["roster"][0]["photo_updated_at"], saved["fetched_at"])
        self.assertEqual(fetch.call_count, 1)

    async def test_first_failure_keeps_missing_portrait_empty(self):
        with patch.object(team_portraits, "fetch_document", side_effect=ValueError("unavailable")):
            result = await team_portraits.enrich_portraits(self.store, squad())
        self.assertEqual(result["roster"][0]["photo_url"], "")

    async def test_directory_records_safe_network_and_empty_page_diagnostics(self):
        for error, code, stage in ((TimeoutError("https://secret@host/path?key=secret"), "timeout", "read"),
                                   (None, "portrait_directory_empty", "parse")):
            self.store.set_meta("team_portraits_v1_barcelona", {})
            with patch.object(team_portraits, "fetch_document", side_effect=error,
                              return_value=(b"<html></html>", URL, "text/html")):
                directory = await team_portraits.portrait_directory(self.store, "barcelona")
            self.assertEqual(directory["status"], "unavailable")
            self.assertFalse(directory.get("cards"))
            self.assertEqual(directory["error_code"], code)
            self.assertEqual(directory["error"]["stage"], stage)
            self.assertNotIn("secret", directory["last_error"])

    async def test_manual_force_retries_the_official_directory_after_a_recent_network_failure(self):
        self.store.set_meta("team_portraits_v1_barcelona", {
            "status": "unavailable", "last_attempt": team_portraits.now(), "cards": []})
        with self.fetch(card()) as fetch:
            result, summary = await team_portraits.enrich_player_photos(self.store, squad(), force=True)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["roster"][0]["photo_match"], "exact_name_and_number")
        self.assertEqual(summary["status"], "completed")

    async def test_old_official_photo_can_refresh_without_refetching_the_roster(self):
        old = squad()
        old["roster"][0].update(photo_url=PHOTO, photo_match="exact_name_and_number", photo_updated_at="2020-01-01T00:00:00+00:00")
        replacement = "https://www.fcbarcelona.com/photo-resources/2026/09/new.jpg"
        with self.fetch(card(image=replacement)) as fetch:
            result = await team_portraits.enrich_portraits(self.store, old)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["roster"][0]["photo_url"], replacement)

    async def test_exact_official_card_upgrades_older_wikimedia_photo(self):
        old = squad()
        old["roster"][0].update(photo_url="https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/Joan.jpg/300px-Joan.jpg",
                                photo_match="wikimedia_verified_club", photo_license="CC BY-SA 4.0",
                                photo_file_page="https://commons.wikimedia.org/wiki/File:Joan.jpg")
        with self.fetch(card()):
            result = await team_portraits.enrich_portraits(self.store, old)
        self.assertEqual(result["roster"][0]["photo_url"], PHOTO)
        self.assertEqual(result["roster"][0]["photo_match"], "exact_name_and_number")
        self.assertFalse(result["roster"][0].get("photo_file_page"))

    def test_only_real_player_card_image_on_fixed_official_host_is_accepted(self):
        for url in ("https://evil.example/person.jpg", "http://www.fcbarcelona.com/photo-resources/person.jpg",
                    "https://www.fcbarcelona.com.evil.example/photo-resources/person.jpg", "https://www.fcbarcelona.com/logo.jpg",
                    "https://secret@www.fcbarcelona.com/photo-resources/person.jpg"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                team_portraits.parse_directory(card(image=url), "barcelona")

    async def test_spurs_lazy_main_portrait_requires_full_name_and_number(self):
        url = team_portraits.DIRECTORIES["tottenham-hotspur"]["url"]
        image = "https://resources.thfc.pulselive.com/photo-resources/2026/08/player.png?width=363"
        html = ('<a class="o-person-pod__inner" href="/player/212319/-richarlison">'
                '<span class="o-person-pod__number">9</span>'
                '<!-- <picture class="o-person-pod__image--main"><img src="' + image + '"></picture> -->'
                '<!-- <picture class="o-person-pod__image--hover"><img src="https://evil.example/action.jpg"></picture> -->'
                '<h4 class="o-person-pod__name">Richarlison</h4></a>')
        snapshot = {"team_id": "tottenham-hotspur", "roster": [
            {"provider_id": "1", "name": "Richarlison", "number": "9", "photo_url": ""},
            {"provider_id": "2", "name": "Other Player", "number": "9", "photo_url": ""}]}
        with patch.object(team_portraits, "fetch_document", return_value=(html.encode(), url, "text/html")):
            result = await team_portraits.enrich_portraits(self.store, snapshot)
        self.assertEqual(result["roster"][0]["photo_url"], image)
        self.assertEqual(result["roster"][0]["photo_source_url"], "https://www.tottenhamhotspur.com/player/212319/-richarlison")
        self.assertFalse(result["roster"][1]["photo_url"])
        snapshot["roster"][0]["number"] = "10"
        result = await team_portraits.enrich_portraits(self.store, snapshot)
        self.assertFalse(result["roster"][0]["photo_url"])
        with self.assertRaises(ValueError):
            team_portraits.parse_directory(html.replace(image, "https://evil.example/player.png"), "tottenham-hotspur")

    async def test_barcelona_directory_runs_first_and_limits_changes_to_selected_players(self):
        snapshot = squad()
        snapshot["roster"][0]["id"] = "espn-player-123"
        snapshot["roster"].append({"id": "espn-player-456", "provider_id": "456", "name": "Second Player", "number": "2", "photo_url": ""})
        with self.fetch(card() + card(first="Second", last="Player", number="2", path="456/second-player")) as fetch:
            result, summary = await team_portraits.enrich_player_photos(self.store, snapshot, player_ids=["123"])
        self.assertEqual(fetch.call_count, 1, "the verified official card removes the need for a player-page request")
        self.assertEqual(summary["attempted_count"], 0)
        self.assertEqual(result["roster"][0]["photo_url"], PHOTO)
        self.assertEqual(result["roster"][1]["photo_url"], "")

    async def test_one_selected_player_cannot_bypass_full_roster_duplicate_name_guard(self):
        snapshot = squad()
        snapshot["roster"].append({**snapshot["roster"][0], "provider_id": "456"})
        with self.fetch(card()):
            result, _ = await team_portraits.enrich_player_photos(self.store, snapshot, player_ids=["123"])
        self.assertTrue(all(not row["photo_url"] for row in result["roster"]))
