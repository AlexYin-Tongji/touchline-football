"""Official portraits require unique full-name and shirt-number identity."""
import asyncio
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from server import team_portraits
from server.storage import Store


URL = team_portraits.DIRECTORIES["barcelona"]["url"]
PHOTO = "https://www.fcbarcelona.com/photo-resources/2026/07/player.jpg?width=470&height=470"
ESPN_PHOTO = "https://a.espncdn.com/i/headshots/soccer/players/full/123.png"


def player_page(player_id="123", team_id="359", photo=ESPN_PHOTO):
    athlete = {"id": player_id, "uid": "s:600~a:" + player_id, "team": {"id": team_id}}
    if photo is not None:
        athlete["headshot"] = {"href": photo}
    return {"routing": {"params": {"id": player_id, "sport": "soccer", "pageType": "player"}},
            "page": {"content": {"player": {"playerId": player_id, "prtlCmnApiRsp": {"athlete": athlete}, "plyrHdr": {"ath": {}}}}}}


def player_html(payload):
    return ("<script>window.__espnfitt__=" + json.dumps(payload) + ";</script>").encode()


def espn_squad(count=1):
    return {"team_id": "arsenal", "provider": "ESPN", "fetched_at": "2026-09-20T10:00:00+00:00",
            "season_id": "eng.1:2026", "roster": [{"id": f"espn-player-{123 + i}", "provider_id": str(123 + i),
            "name": "球员" + str(i), "number": str(i + 1), "photo_url": ""} for i in range(count)]}


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

    def test_player_page_matches_provider_identity_and_accepts_only_explicit_headshot(self):
        parsed = team_portraits.parse_player_portrait(player_html(player_page()), "123", "359")
        self.assertEqual(parsed["photo_url"], ESPN_PHOTO)
        self.assertEqual(parsed["photo_match"], "provider_player_id")
        self.assertEqual(parsed["photo_source_url"], "https://www.espn.com/soccer/player/_/id/123")
        header = player_page(photo=None)
        header["page"]["content"]["player"]["plyrHdr"]["ath"]["headshot"] = ESPN_PHOTO
        self.assertEqual(team_portraits.parse_player_portrait(player_html(header), "123", "359")["photo_url"], ESPN_PHOTO)

    def test_player_page_rejects_routing_player_sport_and_team_mismatches(self):
        for path, replacement in ((["routing", "params", "id"], "999"),
            (["routing", "params", "sport"], "basketball"), (["routing", "params", "pageType"], "team"),
            (["page", "content", "player", "playerId"], "999"),
            (["page", "content", "player", "prtlCmnApiRsp", "athlete", "id"], "999"),
            (["page", "content", "player", "prtlCmnApiRsp", "athlete", "uid"], "s:40~a:123"),
            (["page", "content", "player", "prtlCmnApiRsp", "athlete", "team", "id"], "83")):
            payload = player_page()
            target = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = replacement
            with self.subTest(path=path), self.assertRaises(ValueError):
                team_portraits.parse_player_portrait(player_html(payload), "123", "359")

    def test_player_page_does_not_use_news_team_logos_guessed_or_unsafe_images(self):
        payload = player_page(photo=None)
        player = payload["page"]["content"]["player"]
        player["nws"] = {"image": {"url": ESPN_PHOTO}}
        player["plyrHdr"]["ath"].update(logo=ESPN_PHOTO, img=ESPN_PHOTO)
        self.assertEqual(team_portraits.parse_player_portrait(player_html(payload), "123", "359")["photo_url"], "")
        for photo in ("https://evil.example/person.png", "http://a.espncdn.com/person.png",
                      "https://secret@a.espncdn.com/person.png", "https://a.espncdn.com/i/teamlogos/soccer/500/359.png"):
            with self.subTest(photo=photo):
                self.assertEqual(team_portraits.parse_player_portrait(player_html(player_page(photo=photo)), "123", "359")["photo_url"], "")

    async def test_selected_portrait_enrichment_preserves_core_roster_and_caches_provenance(self):
        snapshot = espn_squad(2)
        original = copy.deepcopy(snapshot)
        url = "https://www.espn.com/soccer/player/_/id/123"
        with patch.object(team_portraits, "fetch_document", return_value=(player_html(player_page()), url, "text/html")) as fetch:
            result, summary = await team_portraits.enrich_player_photos(self.store, snapshot, "359", player_ids=["espn-player-123"])
            cached, second = await team_portraits.enrich_player_photos(self.store, snapshot, "359", player_ids=["123"])
        self.assertEqual(snapshot, original, "the helper returns a copy for atomic persistence")
        self.assertEqual(result["fetched_at"], original["fetched_at"])
        self.assertEqual(result["roster"][1], original["roster"][1])
        self.assertEqual(result["roster"][0]["photo_url"], ESPN_PHOTO)
        self.assertEqual(cached["roster"][0]["photo_source_url"], url)
        self.assertEqual(summary["updated_count"], 1)
        self.assertEqual(summary["selected_available_count"], 1)
        self.assertEqual(summary["missing_count"], 0)
        self.assertEqual(second["cached_count"], 1)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs["allowed_hosts"], {"www.espn.com"})

    async def test_player_pages_bound_requests_and_concurrency_then_continue_uncached_players(self):
        lock = threading.Lock()
        current = 0
        maximum = 0
        def fetch_page(url, **kwargs):
            nonlocal current, maximum
            with lock:
                current += 1
                maximum = max(maximum, current)
            time.sleep(.015)
            with lock:
                current -= 1
            identity = url.rsplit("/", 1)[-1]
            return player_html(player_page(identity)), url, "text/html"
        # One full batch plus two leftovers, so the bound and the continuation
        # are both exercised whatever the configured batch size is.
        squad_size = team_portraits.PLAYER_PAGE_LIMIT + 2
        with patch.object(team_portraits, "fetch_document", side_effect=fetch_page) as fetch:
            first, summary = await team_portraits.enrich_player_photos(self.store, espn_squad(squad_size), "359")
            self.assertEqual(fetch.call_count, team_portraits.PLAYER_PAGE_LIMIT)
            self.assertEqual(summary["remaining_count"], 2)
            second, _ = await team_portraits.enrich_player_photos(self.store, first, "359")
        self.assertEqual(fetch.call_count, squad_size)
        self.assertLessEqual(maximum, 3)
        self.assertGreater(maximum, 1)
        self.assertTrue(all(row.get("photo_url") for row in second["roster"]))

    async def test_player_network_failure_is_cached_and_never_erases_an_existing_portrait(self):
        snapshot = espn_squad()
        snapshot["roster"][0].update(photo_url=ESPN_PHOTO, photo_match="provider_player_id", photo_updated_at="2026-09-20T00:00:00+00:00")
        with patch.object(team_portraits, "fetch_document", side_effect=ValueError("connection failed with private detail")) as fetch:
            result, summary = await team_portraits.enrich_player_photos(self.store, snapshot, "359", force=True)
            missing, second = await team_portraits.enrich_player_photos(self.store, espn_squad(), "359")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result, snapshot)
        self.assertEqual(missing["roster"][0]["photo_url"], "")
        self.assertEqual(summary["status"], "partial")
        self.assertTrue(second["errors"])
        self.assertNotIn("private detail", str(second))

    async def test_missing_headshot_is_cached_and_outsider_player_ids_cannot_fetch(self):
        url = "https://www.espn.com/soccer/player/_/id/123"
        with patch.object(team_portraits, "fetch_document", return_value=(player_html(player_page(photo=None)), url, "text/html")) as fetch:
            result, _ = await team_portraits.enrich_player_photos(self.store, espn_squad(), "359")
            await team_portraits.enrich_player_photos(self.store, espn_squad(), "359")
            with self.assertRaises(ValueError):
                await team_portraits.enrich_player_photos(self.store, espn_squad(), "359", player_ids=["999"])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["roster"][0]["photo_url"], "")

    async def test_redirect_to_other_player_cannot_supply_a_portrait(self):
        with patch.object(team_portraits, "fetch_document", return_value=(player_html(player_page()), "https://www.espn.com/soccer/player/_/id/999", "text/html")):
            result, summary = await team_portraits.enrich_player_photos(self.store, espn_squad(), "359")
        self.assertEqual(result["roster"][0]["photo_url"], "")
        self.assertTrue(summary["errors"])

    async def test_expired_cache_refreshes_and_previous_team_cache_cannot_cross_membership(self):
        calls = []
        def fetch_page(url, **kwargs):
            calls.append(url)
            return player_html(player_page(team_id="359" if len(calls) < 3 else "83")), url, "text/html"
        with patch.object(team_portraits, "fetch_document", side_effect=fetch_page):
            await team_portraits.enrich_player_photos(self.store, espn_squad(), "359")
            with patch.object(team_portraits, "CACHE_SECONDS", 0):
                await team_portraits.enrich_player_photos(self.store, espn_squad(), "359")
            _, summary = await team_portraits.enrich_player_photos(self.store, espn_squad(), "83")
        self.assertEqual(len(calls), 3, "an expired portrait and a different verified club require fresh ID checks")
        self.assertEqual(summary["cached_count"], 0)

    async def test_failed_player_request_retries_after_failure_cache_expires(self):
        with patch.object(team_portraits, "fetch_document", side_effect=ValueError("unavailable")) as fetch:
            await team_portraits.enrich_player_photos(self.store, espn_squad(), "359")
            await team_portraits.enrich_player_photos(self.store, espn_squad(), "359")
            self.assertEqual(fetch.call_count, 1)
            with patch.object(team_portraits, "RETRY_SECONDS", 0):
                await team_portraits.enrich_player_photos(self.store, espn_squad(), "359")
        self.assertEqual(fetch.call_count, 2)

    async def test_concurrent_same_player_requests_share_one_verified_page(self):
        url = "https://www.espn.com/soccer/player/_/id/123"
        with patch.object(team_portraits, "fetch_document", return_value=(player_html(player_page()), url, "text/html")) as fetch:
            results = await asyncio.gather(*(team_portraits.enrich_player_photos(self.store, espn_squad(), "359") for _ in range(3)))
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(all(result["roster"][0]["photo_url"] == ESPN_PHOTO for result, _ in results))

    async def test_barcelona_directory_runs_first_and_limits_changes_to_selected_players(self):
        snapshot = squad()
        snapshot["roster"][0]["id"] = "espn-player-123"
        snapshot["roster"].append({"id": "espn-player-456", "provider_id": "456", "name": "Second Player", "number": "2", "photo_url": ""})
        with self.fetch(card() + card(first="Second", last="Player", number="2", path="456/second-player")) as fetch:
            result, summary = await team_portraits.enrich_player_photos(self.store, snapshot, "83", player_ids=["123"])
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
            result, _ = await team_portraits.enrich_player_photos(self.store, snapshot, "83", player_ids=["123"])
        self.assertTrue(all(not row["photo_url"] for row in result["roster"]))

    async def test_explicit_membership_mismatch_rejects_prior_cached_provider_portrait(self):
        url = "https://www.espn.com/soccer/player/_/id/123"
        with patch.object(team_portraits, "fetch_document", return_value=(player_html(player_page()), url, "text/html")):
            original, _ = await team_portraits.enrich_player_photos(self.store, espn_squad(), "359")
        with patch.object(team_portraits, "fetch_document", return_value=(player_html(player_page(team_id="83")), url, "text/html")) as fetch:
            refreshed, summary = await team_portraits.enrich_player_photos(self.store, original, "359", force=True)
            empty, _ = await team_portraits.enrich_player_photos(self.store, espn_squad(), "359")
        self.assertEqual(fetch.call_count, 1, "a rejected membership still uses the failure retry window")
        self.assertEqual(refreshed["roster"][0]["photo_url"], "")
        self.assertEqual(empty["roster"][0]["photo_url"], "")
        self.assertTrue(summary["errors"])

    async def test_failed_refresh_keeps_existing_photo_but_does_not_reapply_stale_cache_to_missing_photo(self):
        url = "https://www.espn.com/soccer/player/_/id/123"
        with patch.object(team_portraits, "fetch_document", return_value=(player_html(player_page()), url, "text/html")):
            original, _ = await team_portraits.enrich_player_photos(self.store, espn_squad(), "359")
        with patch.object(team_portraits, "fetch_document", side_effect=ValueError("offline")):
            preserved, _ = await team_portraits.enrich_player_photos(self.store, original, "359", force=True)
            empty, summary = await team_portraits.enrich_player_photos(self.store, espn_squad(), "359")
        self.assertEqual(preserved, original)
        self.assertEqual(empty["roster"][0]["photo_url"], "")
        self.assertEqual(summary["selected_available_count"], 0)
        self.assertEqual(summary["missing_count"], 1)

    async def test_canceled_readers_keep_physical_slots_and_same_player_inflight(self):
        release = threading.Event()
        started = []
        lock = threading.Lock()
        def fetch_page(url, **kwargs):
            with lock:
                started.append(url)
            release.wait(3)
            return player_html(player_page(url.rsplit("/", 1)[-1])), url, "text/html"
        async def wait_started(count):
            async with asyncio.timeout(1):
                while len(started) < count:
                    await asyncio.sleep(.005)
        tasks = []
        try:
            with patch.object(team_portraits, "fetch_document", side_effect=fetch_page), patch.object(self.store, "set_meta", wraps=self.store.set_meta) as save:
                first = asyncio.create_task(team_portraits.enrich_player_photos(self.store, espn_squad(3), "359"))
                tasks.append(first)
                await wait_started(3)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(first, .2)
                same = asyncio.create_task(team_portraits.enrich_player_photos(self.store, espn_squad(), "359"))
                next_players = asyncio.create_task(team_portraits.enrich_player_photos(self.store, espn_squad(6), "359", player_ids=["126", "127", "128"]))
                tasks.extend([same, next_players])
                await asyncio.sleep(.05)
                self.assertEqual(len(started), 3, "abandoned physical requests still occupy all three slots")
                save.assert_not_called()
                release.set()
                await asyncio.wait_for(asyncio.gather(same, next_players), 1)
                self.assertEqual(started.count("https://www.espn.com/soccer/player/_/id/123"), 1,
                                 "a replacement reader shares its predecessor's running worker")
                saved_keys = [call.args[0] for call in save.call_args_list]
                self.assertNotIn("player_portrait_v1_359_124", saved_keys)
                self.assertNotIn("player_portrait_v1_359_125", saved_keys)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_timed_out_workers_keep_slots_until_physical_completion(self):
        release = threading.Event()
        started = []
        lock = threading.Lock()
        def fetch_page(url, **kwargs):
            with lock:
                started.append(url)
            release.wait(3)
            return player_html(player_page(url.rsplit("/", 1)[-1])), url, "text/html"
        original_wait_for = asyncio.wait_for
        async def short_page_timeout(awaitable, timeout):
            return await original_wait_for(awaitable, .03 if timeout == 22 else timeout)
        task = None
        try:
            with patch.object(team_portraits, "fetch_document", side_effect=fetch_page), patch.object(team_portraits.asyncio, "wait_for", side_effect=short_page_timeout):
                _, summary = await original_wait_for(team_portraits.enrich_player_photos(self.store, espn_squad(3), "359"), 1)
                self.assertEqual(summary["status"], "unavailable")
                task = asyncio.create_task(team_portraits.enrich_player_photos(self.store, espn_squad(6), "359", player_ids=["126", "127", "128"]))
                await asyncio.sleep(.05)
                self.assertEqual(len(started), 3, "timing out an async reader cannot create new physical capacity")
                release.set()
                await original_wait_for(task, 1)
        finally:
            release.set()
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_directory_first_and_espn_enrichment_share_one_physical_pool(self):
        release = threading.Event()
        started = []
        lock = threading.Lock()
        active = maximum = 0
        def fetch_page(url, **kwargs):
            nonlocal active, maximum
            with lock:
                started.append(url)
                active += 1
                maximum = max(maximum, active)
            release.wait(3)
            with lock:
                active -= 1
            if url == URL:
                return card().encode(), url, "text/html"
            return player_html(player_page(url.rsplit("/", 1)[-1])), url, "text/html"
        async def wait_started(count):
            async with asyncio.timeout(1):
                while len(started) < count:
                    await asyncio.sleep(.005)
        tasks = []
        try:
            with patch.object(team_portraits, "fetch_document", side_effect=fetch_page):
                directory = asyncio.create_task(team_portraits.enrich_player_photos(self.store, squad(), "83"))
                tasks.append(directory)
                await wait_started(1)
                self.assertEqual(started, [URL])
                espn = asyncio.create_task(team_portraits.enrich_player_photos(self.store, espn_squad(3), "359"))
                tasks.append(espn)
                await wait_started(3)
                await asyncio.sleep(.05)
                self.assertEqual(len(started), 3, "initializing player locks must not reset the active directory's pool")
                self.assertEqual(sum(url != URL for url in started), 2)
                release.set()
                await asyncio.wait_for(asyncio.gather(*tasks), 1)
                self.assertEqual(len(started), 4)
                self.assertLessEqual(maximum, 3)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def test_player_page_rejects_ambiguous_payload_and_conflicting_header_identity(self):
        header = player_page(photo=None)
        header["page"]["content"]["player"]["plyrHdr"]["ath"].update(headshot=ESPN_PHOTO, tmUid="s:600~t:83")
        for body in (player_html(player_page()) + player_html(player_page()), player_html(header), b"<script>window.__espnfitt__=not_json;</script>"):
            with self.subTest(body=body[:50]), self.assertRaises(ValueError):
                team_portraits.parse_player_portrait(body, "123", "359")


if __name__ == "__main__":
    unittest.main()
