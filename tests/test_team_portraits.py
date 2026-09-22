"""Official portraits require unique full-name and shirt-number identity."""
import asyncio
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

    def test_only_real_player_card_image_on_fixed_official_host_is_accepted(self):
        for url in ("https://evil.example/person.jpg", "http://www.fcbarcelona.com/photo-resources/person.jpg",
                    "https://www.fcbarcelona.com.evil.example/photo-resources/person.jpg", "https://www.fcbarcelona.com/logo.jpg",
                    "https://secret@www.fcbarcelona.com/photo-resources/person.jpg"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                team_portraits.parse_directory(card(image=url), "barcelona")


if __name__ == "__main__":
    unittest.main()
