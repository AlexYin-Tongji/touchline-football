"""Commons portraits anchored on the roster ESPN id, never on a name search."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server import community_portraits, team_portraits, wikidata_portraits, wikimedia_portraits
from server.storage import Store


PLAYER = {"provider_id": "196176", "name": "David Raya", "nationality": "西班牙",
          "birth_date": "1995-09-15"}
FILE_NAME = "David Raya.jpg"


def claims(prop, value, *, rank="normal", precision=None):
    if prop == "P569":
        stored = {"time": value, "precision": precision}
    else:
        stored = value
    return {"claims": {prop: [{"rank": rank, "mainsnak": {"datavalue": {"value": stored}}}]}}


def search(qid="Q19665907", total=1):
    hits = [{"title": qid}] if total else []
    if total == 2:
        hits = [{"title": "Q1"}, {"title": "Q2"}]
    return {"query": {"searchinfo": {"totalhits": total}, "search": hits}}


def commons(file_name=FILE_NAME, license_name="CC BY-SA 4.0", license_code="cc-by-sa-4.0"):
    return {"query": {"pages": {"1": {"title": "File:" + file_name, "imageinfo": [{
        "thumburl": "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/David_Raya.jpg/300px-David_Raya.jpg",
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:" + file_name.replace(" ", "_"),
        "extmetadata": {"LicenseShortName": {"value": license_name},
                        "License": {"value": license_code},
                        "Artist": {"value": "Example Photographer"},
                        "Restrictions": {"value": ""}}}]}}}}


class EspnAnchoredPortraitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "wikidata.sqlite3")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def route(self, *, total=1, espn="196176", birth="+1995-09-15T00:00:00Z", precision=11,
              file_name=FILE_NAME, license_name="CC BY-SA 4.0", license_code="cc-by-sa-4.0",
              p18_rank="normal", extra_p18=()):
        tables = {
            "P3681": claims("P3681", espn),
            "P569": claims("P569", birth, precision=precision),
            "P18": claims("P18", file_name, rank=p18_rank),
        }
        if extra_p18:
            tables["P18"]["claims"]["P18"].append(
                {"rank": "normal", "mainsnak": {"datavalue": {"value": extra_p18}}})

        async def answer(_store, host, params):
            if params.get("list") == "search":
                self.assertIn("haswbstatement:P3681=196176", params.get("srsearch", ""))
                self.assertNotIn(PLAYER["name"], params.get("srsearch", ""))
                return search(total=total)
            if params.get("action") == "wbgetclaims":
                return tables[params["property"]]
            if host == "commons.wikimedia.org":
                return commons(file_name, license_name, license_code)
            raise AssertionError((host, params))

        return answer

    async def resolve(self, **kwargs):
        with patch.object(wikimedia_portraits, "request_json", side_effect=self.route(**kwargs)) as requests:
            result = await wikidata_portraits.resolve_portrait(self.store, PLAYER, force=True)
        return result, requests

    async def test_a_matching_espn_id_and_free_file_is_accepted(self):
        result, requests = await self.resolve()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["photo_match"], "wikidata_espn_provider_id")
        self.assertTrue(result["photo_url"].startswith("https://thumb.wikimedia.org/"))
        self.assertEqual(result["photo_license"], "CC BY-SA 4.0")
        self.assertEqual(result["photo_source_url"], "https://www.wikidata.org/wiki/Q19665907")
        self.assertGreaterEqual(requests.await_count, 4)
        self.assertIsNotNone(wikidata_portraits.cached_portrait(self.store, PLAYER))
        with patch.object(wikimedia_portraits, "request_json", side_effect=AssertionError("cached portrait")):
            again = await wikidata_portraits.resolve_portrait(self.store, PLAYER)
        self.assertEqual(again["photo_url"], result["photo_url"])

    async def test_a_birth_date_conflict_attaches_nothing(self):
        result, _requests = await self.resolve(birth="+1990-01-01T00:00:00Z")
        self.assertNotEqual(result["status"], "completed")
        self.assertFalse(result.get("photo_url"))
        self.assertEqual(result["reason_code"], "wikidata_birth_conflict")
        self.assertNotIn("namesake_blocked", result)

    async def test_two_entities_for_one_id_attach_nothing(self):
        result, requests = await self.resolve(total=2)
        self.assertEqual(result["reason_code"], "wikidata_no_espn_id")
        self.assertFalse(result.get("photo_url"))
        self.assertEqual(requests.await_count, 1)

    async def test_a_claim_that_does_not_repeat_the_id_is_refused(self):
        result, _requests = await self.resolve(espn="1")
        self.assertFalse(result.get("photo_url"))
        self.assertEqual(result["reason_code"], "rejected")

    async def test_a_non_free_file_is_refused(self):
        result, _requests = await self.resolve(license_name="All rights reserved", license_code="arr")
        self.assertFalse(result.get("photo_url"))
        self.assertEqual(result["reason_code"], "license_not_free")

    async def test_two_ordinary_portraits_are_not_guessed_between(self):
        result, _requests = await self.resolve(extra_p18="Other Face.jpg")
        self.assertFalse(result.get("photo_url"))
        self.assertEqual(result["reason_code"], "no_free_image")

    async def test_the_anchored_photo_is_applied_before_the_name_search(self):
        photo = {"status": "completed", "photo_url": "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/A.jpg/300px-A.jpg",
                 "photo_credit": "Example", "photo_source_url": "https://www.wikidata.org/wiki/Q19665907",
                 "photo_file_page": "https://commons.wikimedia.org/wiki/File:A.jpg",
                 "photo_license": "CC BY-SA 4.0", "photo_match": "wikidata_espn_provider_id",
                 "fetched_at": "2026-10-14T00:00:00+00:00"}
        squad = {"team_id": "arsenal", "roster": [{**PLAYER, "photo_url": ""}]}
        with patch.object(team_portraits, "_enrich_espn_directory", new=AsyncMock(return_value={"attempted_count": 0, "errors": []})), \
                patch.object(wikidata_portraits, "resolve_portrait", new=AsyncMock(return_value=photo)) as anchored, \
                patch.object(community_portraits, "resolve_portrait", new=AsyncMock(side_effect=AssertionError("name search"))) as community, \
                patch.object(wikimedia_portraits, "resolve_portrait", new=AsyncMock(side_effect=AssertionError("wikipedia search"))):
            result, summary = await team_portraits.enrich_player_photos(self.store, squad)
        self.assertEqual(anchored.await_count, 1)
        self.assertEqual(community.await_count, 0)
        self.assertEqual(result["roster"][0]["photo_match"], "wikidata_espn_provider_id")
        self.assertEqual(summary["wikidata_espn_attempted_count"], 1)
        self.assertEqual(summary["community_attempted_count"], 0)


if __name__ == "__main__":
    unittest.main()
