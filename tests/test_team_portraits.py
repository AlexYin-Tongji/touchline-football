"""Official portraits require unique full-name and shirt-number identity."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import team_portraits
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
        def fetch_page(url, **kwargs):
            return ((card().encode(), URL, "text/html") if url == URL else
                    (player_html(player_page(team_id="83", photo=None)), url, "text/html"))
        with patch.object(team_portraits, "fetch_document", side_effect=fetch_page):
            result, _ = await team_portraits.enrich_player_photos(self.store, snapshot, player_ids=["123"])
        self.assertTrue(all(not row["photo_url"] for row in result["roster"]))

