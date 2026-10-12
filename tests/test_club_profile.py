"""Club identity fields: one source, one licence, and never a guess.

The failure these guard against is not "a field stayed empty". It is a
plausible wrong entity being written into the record with a real-looking
founding year, which is invisible afterwards because the value looks correct.
Every case below is one that live Wikidata actually returned while this was
written: "Bologna" is a city, "Nice" is a place, "Monaco" is a country, and
Chelsea's second search hit is the academy.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server import club_profile
from server.storage import Store

FOUNDED = {"P571": [{"mainsnak": {"datavalue": {"value": {"time": "+1886-01-01T00:00:00Z"}}}}]}
VENUE = {"P115": [{"mainsnak": {"datavalue": {"value": {"id": "Q163995"}}}}]}
NICKNAME = {"P1449": [{"mainsnak": {"datavalue": {"value": {"text": "The Gunners"}}}}]}


def entity(qid, label, description, classes, claims=None):
    return {"id": qid,
            "labels": {"en": {"value": label, "language": "en"}},
            "descriptions": {"en": {"value": description, "language": "en"}},
            "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": code}}}} for code in classes],
                       **(claims or {})}}


class IdentityRulesTests(unittest.TestCase):
    def test_a_club_and_its_academy_are_told_apart(self):
        academy = entity("Q2669653", "Chelsea F.C. Development Squad and Academy",
                         "association football club", ["Q103229495"], FOUNDED)
        senior = entity("Q9617", "Chelsea F.C.", "association football club", ["Q103229495"], FOUNDED)
        self.assertIsNone(club_profile.identity_score(academy))
        self.assertEqual(club_profile.identity_score(senior), 2)

    def test_namesakes_that_share_a_name_with_a_club_are_refused(self):
        # Each of these is returned by the same search and is not the club.
        for qid, label, description, classes in (
                ("Q1891", "Bologna", "city in Italy", ["Q515"]),
                ("Q33959", "Nice", "commune in Alpes-Maritimes, France", ["Q484170"]),
                ("Q235", "Monaco", "sovereign city-state", ["Q3624078"])):
            with self.subTest(label=label):
                self.assertIsNone(club_profile.identity_score(
                    entity(qid, label, description, classes, FOUNDED)))

    def test_reserve_youth_and_womens_sides_are_refused_whatever_their_class(self):
        for label, description, classes in (
                ("Arsenal F.C. Under-21s", "reserve team", ["Q476028"]),
                ("Chelsea Women", "women's association football team", ["Q476028"]),
                ("Bayern Munich II", "association football club", ["Q103229495"])):
            with self.subTest(label=label):
                self.assertIsNone(club_profile.identity_score(
                    entity("Q1", label, description, classes, FOUNDED)))

    def test_an_entity_with_nothing_to_collect_is_refused(self):
        # Both are "football clubs"; neither states a founding year, a venue or
        # a nickname, so accepting one could only put the wrong name in place.
        self.assertIsNone(club_profile.identity_score(
            entity("Q2", "Some F.C.", "football club", ["Q476028"])))

    def test_a_nickname_in_another_script_is_dropped_not_transliterated(self):
        # Aston Villa's Wikidata nickname really is Greek. A pass that invented
        # a romanisation would file it as a published name.
        self.assertEqual(club_profile._latin("Οι Χουριάτες"), "")
        self.assertEqual(club_profile._latin("Арсенал"), "")
        self.assertEqual(club_profile._latin("The Gunners"), "The Gunners")

    def test_only_the_year_is_taken_from_an_unknown_day(self):
        facts = club_profile.facts_from(
            entity("Q9617", "Arsenal F.C.", "football club", ["Q103229495"],
                   {"P571": [{"mainsnak": {"datavalue": {"value": {"time": "+1886-00-00T00:00:00Z"}}}}]}))
        self.assertEqual(facts["founded"], 1886)

    def test_a_venue_entity_without_an_english_label_is_not_written(self):
        found = entity("Q9617", "Arsenal F.C.", "football club", ["Q103229495"], {**FOUNDED, **VENUE})
        self.assertNotIn("stadium", club_profile.facts_from(found, labels={}))
        self.assertEqual(club_profile.facts_from(found, labels={"Q163995": "Emirates Stadium"})["stadium"],
                         "Emirates Stadium")


class ApplyRulesTests(unittest.TestCase):
    def test_a_stated_value_is_never_overwritten(self):
        team = {"id": "arsenal", "founded": 1886}
        written = club_profile.apply_facts(team, {"founded": 1900, "stadium": "Highbury"}, qid="Q9617")
        self.assertEqual(written, ["stadium"])
        self.assertEqual(team["founded"], 1886)

    def test_a_blank_is_not_written(self):
        team = {"id": "arsenal"}
        self.assertEqual(club_profile.apply_facts(team, {"founded": None, "stadium": ""}, qid="Q9617"), [])
        self.assertNotIn("stadium", team)

    def test_a_written_value_carries_its_source_on_the_same_write(self):
        team = {"id": "arsenal"}
        club_profile.apply_facts(team, {"founded": 1886}, qid="Q9617")
        source = team["background_sources"]["wikidata"]
        self.assertEqual(source["entity"], "Q9617")
        self.assertEqual(source["license"], "CC0")
        self.assertEqual(source["url"], "https://www.wikidata.org/wiki/Q9617")
        self.assertEqual(source["fields"], ["founded"])

    def test_missing_fields_ignores_placeholders_but_not_zero(self):
        self.assertEqual(club_profile.missing_fields({"founded": 1886, "stadium": "X"}), ("nickname",))
        self.assertEqual(club_profile.missing_fields({"founded": 1886, "stadium": "X", "nickname": "Y"}), ())
        self.assertEqual(club_profile.missing_fields({"founded": 0}), ("founded", "stadium", "nickname"))


class CrestStateTests(unittest.TestCase):
    def test_a_complete_local_asset_reports_fresh(self):
        state = club_profile.crest_state({"crest_url": "/assets/clubs/arsenal.png",
                                          "crest_source_url": "https://www.espn.com/soccer/team/_/id/359",
                                          "crest_attribution": "取自 ESPN 球队页面"})
        self.assertEqual(state["status"], "fresh")
        self.assertTrue(state["local_asset"])

    def test_a_crest_missing_its_provenance_is_reported_not_downloaded(self):
        state = club_profile.crest_state({"crest_url": "/assets/clubs/arsenal.png"})
        self.assertEqual(state["status"], "missing")
        self.assertEqual(state["missing"], ["crest_source_url", "crest_attribution"])

    def test_a_remote_crest_is_flagged_rather_than_treated_as_local(self):
        state = club_profile.crest_state({"crest_url": "https://example.test/badge.png",
                                          "crest_source_url": "https://example.test",
                                          "crest_attribution": "某处"})
        self.assertEqual(state["status"], "remote")
        self.assertFalse(state["local_asset"])


class CollectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "profile.sqlite3")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def test_a_complete_record_costs_no_lookup_at_all(self):
        complete = {"id": "arsenal", "founded": 1886, "stadium": "Emirates Stadium", "nickname": "The Gunners"}
        with patch.object(club_profile, "search_entities", new=AsyncMock()) as search:
            result = await club_profile.collect(self.store, dict(complete))
        search.assert_not_awaited()
        self.assertEqual(result["written"], [])
        self.assertEqual(result["summary"]["status"], "completed")

    async def test_a_search_that_finds_only_the_wrong_thing_changes_nothing(self):
        team = {"id": "bologna", "english_name": "Bologna", "aliases": ["Bologna FC 1909"]}
        async def search(store, term, **kwargs):
            return ["Q1891"]
        async def candidate(store, qid, **kwargs):
            return entity("Q1891", "Bologna", "city in Italy", ["Q515"], FOUNDED)
        with patch.object(club_profile, "search_entities", new=search), \
             patch.object(club_profile, "entity", new=candidate):
            result = await club_profile.collect(self.store, dict(team))
        self.assertEqual(result["written"], [])
        self.assertEqual(result["summary"]["status"], "missing")
        self.assertEqual(result["summary"]["reason_code"], "no_candidate")
        self.assertNotIn("founded", result["team"])

    async def test_the_senior_club_wins_over_a_higher_ranked_academy(self):
        team = {"id": "chelsea", "english_name": "Chelsea"}
        academy = entity("Q2669653", "Chelsea F.C. Development Squad and Academy",
                         "association football club", ["Q103229495"], FOUNDED)
        senior = entity("Q9617", "Chelsea F.C.", "association football club", ["Q103229495"],
                        {**FOUNDED, **VENUE})
        async def search(store, term, **kwargs):
            return ["Q2669653", "Q9617"]
        async def candidate(store, qid, **kwargs):
            return {"Q2669653": academy, "Q9617": senior}[qid]
        async def labels(store, qids):
            return {"Q163995": "Stamford Bridge"}
        with patch.object(club_profile, "search_entities", new=search), \
             patch.object(club_profile, "entity", new=candidate), \
             patch.object(club_profile, "labels_for", new=labels):
            result = await club_profile.collect(self.store, dict(team))
        self.assertEqual(result["qid"], "Q9617")
        self.assertEqual(result["team"]["founded"], 1886)
        self.assertEqual(result["team"]["stadium"], "Stamford Bridge")


if __name__ == "__main__":
    unittest.main()
