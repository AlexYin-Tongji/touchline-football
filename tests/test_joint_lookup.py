"""The surname search is a second door onto the same player.

Measured 2026-10-12 against the free API tier: the search endpoint returns one
record per query and does not order by relevance, so a player whose name is
shared with someone else is not merely unpicked by the checker — their record
is absent from the response. The full-name query cannot reach them. The surname
query returns a *different* set, and the roster's own ESPN athlete id is the one
fact that can confirm a record from either set.

This is the "joint lookup" the maintainer asked for: ESPN's roster supplies the
id, the publisher's per-id record states `idESPN`, and a record agreeing on that
id is the same athlete by construction rather than by name inference.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server import community_portraits
from server.storage import Store


CUTOUT = "https://r2.thesportsdb.com/images/media/player/cutout/abc123.png"
SHADOWED_ID = "34145645"      # the record the full-name query returns
WANTED_ID = "34148411"        # the record only the surname query returns


def search(*records):
    return {"player": list(records)}


def record(name, publisher_id, nationality="England", **extra):
    return {"idPlayer": publisher_id, "strPlayer": name, "strNationality": nationality,
            "strSport": "Soccer", "strStatus": "Active", "strCutout": CUTOUT, **extra}


class JointLookupTests(unittest.IsolatedAsyncioTestCase):
    """Two searches, one per-id read, and the id decides."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "joint.sqlite3")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def resolve(self, by_name, by_surname, player, full_records=()):
        """Answer the name search and the surname search differently."""
        async def search_for(_store, term):
            return by_surname if term == community_portraits._surname(player["name"]) else by_name

        async def lookup(_store, publisher_id):
            for row in full_records:
                if str(row.get("idPlayer")) == str(publisher_id):
                    return {"players": [row]}
            return {"players": []}

        with patch.object(community_portraits, "_search", new=AsyncMock(side_effect=search_for)), \
             patch.object(community_portraits, "_lookup", new=AsyncMock(side_effect=lookup)):
            return await community_portraits.resolve_portrait(self.store, player, "chelsea", force=True)

    def chelsea_james(self):
        # The Chelsea player, born 1999-12-08. The full-name search answers with
        # the Sheffield Wednesday namesake; the surname search returns another
        # record that states the roster's own ESPN id.
        return {"provider_id": "189007", "name": "Reece James", "nationality": "英格兰",
                "birth_date": "1999-12-08"}

    async def test_a_record_from_the_surname_search_can_carry_the_match(self):
        name_hit = search(record("Reece James", SHADOWED_ID, dateBorn="1993-11-07"))
        surname_hit = search(record("James McArthur", WANTED_ID, nationality="Scotland", dateBorn="1987-10-07"))
        full = {"idPlayer": WANTED_ID, "strPlayer": "Reece James", "strNationality": "England",
                "strSport": "Soccer", "strStatus": "Active", "dateBorn": "1999-12-08",
                "idESPN": "189007", "strCutout": CUTOUT}
        result = await self.resolve(name_hit, surname_hit, self.chelsea_james(), (full,))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["photo_url"], CUTOUT)
        self.assertEqual(result["photo_match"], "community_espn_provider_id")

    async def test_the_surname_search_is_not_accepted_on_its_own(self):
        # Without the id agreeing, the surname route publishes nothing: the
        # date check in that tier is what makes a partial name safe, and the
        # scotland-born record does not match the roster's date.
        name_hit = search(record("Reece James", SHADOWED_ID, dateBorn="1993-11-07"))
        surname_hit = search(record("James McArthur", WANTED_ID, nationality="Scotland", dateBorn="1987-10-07"))
        full = {"idPlayer": WANTED_ID, "strPlayer": "James McArthur", "strNationality": "Scotland",
                "strSport": "Soccer", "strStatus": "Active", "dateBorn": "1987-10-07",
                "idESPN": "999999", "strCutout": CUTOUT}
        result = await self.resolve(name_hit, surname_hit, self.chelsea_james(), (full,))
        self.assertNotEqual(result["status"], "completed")
        self.assertFalse(result.get("photo_url"))

    async def test_the_id_bound_read_is_bounded_by_the_candidate_cap(self):
        # A common surname returns a long list, and each candidate costs a
        # lookup. The tier reads at most MAX_PROVIDER_LOOKUPS of them.
        name_hit = search(record("Reece James", SHADOWED_ID, dateBorn="1993-11-07"))
        surname_hit = search(*[record("Reece James", str(34100000 + index), dateBorn="1993-11-07")
                               for index in range(community_portraits.MAX_PROVIDER_LOOKUPS + 1)])
        lookups = []

        async def lookup(_store, publisher_id):
            lookups.append(str(publisher_id))
            return {"players": []}

        async def search_for(_store, term):
            return surname_hit if term == "James" else name_hit

        with patch.object(community_portraits, "_search", new=AsyncMock(side_effect=search_for)), \
             patch.object(community_portraits, "_lookup", new=AsyncMock(side_effect=lookup)):
            await community_portraits.resolve_portrait(self.store, self.chelsea_james(), "chelsea", force=True)
        self.assertLessEqual(len(lookups), community_portraits.MAX_PROVIDER_LOOKUPS)

    async def test_a_roster_without_an_espn_id_spends_no_lookup_on_the_surname_hit(self):
        name_hit = search(record("Reece James", SHADOWED_ID, dateBorn="1993-11-07"))
        surname_hit = search(record("James McArthur", WANTED_ID, nationality="Scotland", dateBorn="1987-10-07"))
        lookups = []

        async def lookup(_store, publisher_id):
            lookups.append(str(publisher_id))
            return {"players": []}

        async def search_for(_store, term):
            return surname_hit if term == "James" else name_hit

        player = {**self.chelsea_james(), "provider_id": ""}
        with patch.object(community_portraits, "_search", new=AsyncMock(side_effect=search_for)), \
             patch.object(community_portraits, "_lookup", new=AsyncMock(side_effect=lookup)):
            result = await community_portraits.resolve_portrait(self.store, player, "chelsea", force=True)
        self.assertEqual(lookups, [])
        self.assertFalse(result.get("photo_url"))


if __name__ == "__main__":
    unittest.main()
