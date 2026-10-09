"""The catalogue enrichment must never invent a value or overwrite a review.

`scripts/enrich-clubs.py` edits the one file the whole application reads club
facts from, so what is worth testing is not the network calls but the rules
around them: which value is taken, which is refused, and what happens to a field
a reviewer already filled in.
"""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location("enrich_clubs", ROOT / "scripts/enrich-clubs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EnrichmentRulesTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_a_nickname_in_another_script_is_refused(self):
        """Wikidata holds the statement in whatever language it was entered in.

        Aston Villa's nickname arrives as the Greek "Οι Χωριάτες" and Ipswich's
        as "Οι Τρακτεράδες". Storing either beside a Chinese fact table shows a
        reader a script the rest of the page does not use, and no nickname is
        better than an incongruous one.
        """
        for value in ("Οι Χωριάτες", "Οι Τρακτεράδες", "一二三", "ФК Зенит", "", "   ", ".."):
            with self.subTest(value=value):
                self.assertIsNone(self.module.latin_value(value))
        for value in ("The Cherries", "Rossoblù", "Die Roten", "Los Blancos", "Blaugrana", "le SCO"):
            with self.subTest(value=value):
                self.assertEqual(self.module.latin_value(value), value)

    def test_a_club_name_is_searched_in_the_shapes_the_club_goes_by(self):
        """The catalogue holds the name a reader recognises, not the page title.

        "Chelsea" is the club's article only as "Chelsea F.C.", "Lens" only as
        "RC Lens", "Nice" only as "OGC Nice" — and all three short forms are
        ordinary words that match a place or a common noun first. The reviewed
        `aliases` are what the club is actually called, so they are tried after
        the display name and before giving up.
        """
        terms = self.module.search_terms({"english_name": "Lens", "aliases": ["朗斯RC", "RC Lens"]})
        self.assertEqual(terms[0], "Lens", "目录里的名称必须排在第一位")
        self.assertIn("RC Lens", terms)
        self.assertNotIn("朗斯RC", terms, "中文别名无法匹配英文检索索引")

        terms = self.module.search_terms({"english_name": "Chelsea", "aliases": ["车路士", "Chelsea FC"]})
        self.assertEqual(terms, ["Chelsea", "Chelsea FC"])

        self.assertEqual(self.module.search_terms({"english_name": ""}), [])
        self.assertEqual(self.module.search_terms({}), [])

    def test_only_a_positive_identification_yields_a_founding_year(self):
        """Namesakes are the whole difficulty: Bologna is a city, Nice a place.

        The class claim decides, and no candidate means a reported gap. An
        inception with an unknown day is stored as "+1909-00-00T00:00:00Z", so
        only the year is read — never a month or day the source did not state.
        """
        claims = {"P571": [{"mainsnak": {"datavalue": {"value": {"time": "+1909-00-00T00:00:00Z"}}}}]}
        with patch.object(self.module, "resolve", return_value=("Q1893", "resolved", claims)):
            qid, _, facts = self.module.facts_for({"english_name": "Bologna"})
        self.assertEqual(qid, "Q1893")
        self.assertEqual(facts["founded"], 1909)
        self.assertNotIn("stadium", {k: v for k, v in facts.items() if isinstance(v, str)},
                         "官网名称由后面的批量调用填入")

        with patch.object(self.module, "resolve", return_value=(None, "not_a_football_club", {})):
            qid, status, facts = self.module.facts_for({"english_name": "Nice", "aliases": ["OGC Nice"]})
        self.assertIsNone(qid)
        self.assertEqual(status, "not_a_football_club")
        self.assertEqual(facts, {})

    def test_a_year_that_is_not_a_year_is_refused(self):
        for raw in ("+0000-00-00T00:00:00Z", "+1500-00-00T00:00:00Z"):
            claims = {"P571": [{"mainsnak": {"datavalue": {"value": {"time": raw}}}}]}
            with self.subTest(raw=raw):
                with patch.object(self.module, "resolve", return_value=("Q1", "resolved", claims)):
                    _, _, facts = self.module.facts_for({"english_name": "X"})
                self.assertNotIn("founded", facts)
        with patch.object(self.module, "resolve", return_value=("Q1", "resolved", {})):
            _, _, facts = self.module.facts_for({"english_name": "X"})
        self.assertEqual(facts, {})

    def test_a_field_the_catalogue_already_states_is_never_overwritten(self):
        """A reviewer's value wins, and provenance is recorded for what is added.

        The catalogue is the reviewed record; a pass that quietly replaced a
        checked founding year would leave no way to tell which value had been
        approved. A venue with no usable name is dropped rather than stored
        blank, because an empty stadium renders as an empty fact row.
        """
        clubs = [
            {"id": "arsenal", "english_name": "Arsenal", "founded": 1886},
            {"id": "angers", "english_name": "Angers", "city": "昂热"},
            {"id": "bologna", "english_name": "Bologna"},
        ]
        added = {
            "arsenal": {"qid": "Q9617", "founded": 1999,
                        "stadium": {"value": "Emirates Stadium", "entity": "Q207874"}},
            "angers": {"qid": "Q845137", "founded": 1919,
                       "stadium": {"entity": "Q387213"}},
            "bologna": {"qid": "Q1893", "nickname": "Rossoblù",
                        "stadium": {"value": "Stadio Renato Dall'Ara", "entity": "Q1061544"}},
        }
        notes = {"clubs": [{"id": "arsenal"}]}
        written = self.module.apply_facts(clubs, added, notes, reviewed_at="2026-10-08")
        self.assertEqual(written, 3)
        by_id = {club["id"]: club for club in clubs}
        self.assertEqual(by_id["arsenal"]["founded"], 1886, "已审阅的成立年不得被覆盖")
        self.assertEqual(by_id["arsenal"]["stadium"], "Emirates Stadium")
        self.assertEqual(by_id["angers"]["founded"], 1919)
        self.assertEqual(by_id["angers"]["city"], "昂热", "其他字段保持不变")
        self.assertNotIn("stadium", by_id["angers"], "没有名称的场馆不写入")
        self.assertEqual(by_id["bologna"]["nickname"], "Rossoblù")
        for club in clubs:
            source = club["background_sources"]["wikidata"]
            self.assertEqual(source["license"], "CC0")
            self.assertEqual(source["reviewed_at"], "2026-10-08")
            self.assertTrue(source["entity"].startswith("Q"))
        self.assertEqual(notes["clubs"][0]["background_sources"]["entity"], "Q9617")

    def test_an_unresolved_club_is_reported_and_contributes_nothing(self):
        """A gap is a result. Guessing a namesake would be the one unrecoverable
        mistake: a wrong founding year looks exactly like a right one."""
        clubs = [{"id": "monaco", "english_name": "AS Monaco"}]
        written = self.module.apply_facts(clubs, {"monaco": {"qid": "Q180305"}}, {"clubs": []})
        self.assertEqual(written, 1)
        self.assertNotIn("founded", clubs[0])
        self.assertNotIn("stadium", clubs[0])
        with patch.object(self.module, "resolve", return_value=(None, "not_a_football_club", {})):
            qid, _, facts = self.module.facts_for({"english_name": "Monaco"})
        self.assertIsNone(qid)
        self.assertEqual(facts, {})


class ClubIdentityScoringTests(unittest.TestCase):
    """Choosing the senior club, not the first entity that looks like a club.

    Chelsea is the case that produced these tests. Its search results begin with
    the club itself, but the club's P31 is `Q103229495` — plain "association
    football club" — which the first version of this pass did not list, so the
    candidate was skipped and the pass settled on the third result, "Chelsea
    F.C. Development Squad and Academy". Both are football clubs; only one is
    the first team. A missing class silently converted a right answer into a
    wrong one, so the scorer is tested against the real claim shapes.
    """

    # The shapes taken from Wikidata on 2026-10-08.
    CHELSEA = {"claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q103229495"}}}}],
                          "P571": [{"mainsnak": {"datavalue": {"value": {"time": "+1905-00-00T00:00:00Z"}}}}],
                          "P115": [{"mainsnak": {"datavalue": {"value": {"id": "Q171458"}}}}]},
               "descriptions": {"en": {"value": "association football club in London, England"}}}
    ACADEMY = {"claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q476028"}}}},
                                  {"mainsnak": {"datavalue": {"value": {"id": "Q134468886"}}}}],
                          "P571": [{"mainsnak": {"datavalue": {"value": {"time": "+1948-00-00T00:00:00Z"}}}}],
                          "P115": [{"mainsnak": {"datavalue": {"value": {"id": "Q171458"}}}}]},
               "descriptions": {"en": {"value": "football club"}}}
    WOMEN = {"claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q28140340"}}}}],
                        "P571": [{"mainsnak": {"datavalue": {"value": {"time": "+1992-00-00T00:00:00Z"}}}}],
                        "P115": [{"mainsnak": {"datavalue": {"value": {"id": "Q171458"}}}}]},
             "descriptions": {"en": {"value": "women's association football club based in London"}}}
    HISTORY = {"claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q106842007"}}}}]},
               "descriptions": {"en": {"value": "aspect of history"}}}
    SHELL = {"claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q476028"}}}}]},
             "descriptions": {"en": {"value": "football club"}}}

    def setUp(self):
        self.module = load_module()

    def test_the_club_scores_above_a_reserve_side_that_looks_identical(self):
        """The academy entity states no give-away in its prose — only its class."""
        club = self.module.club_identity_score(self.CHELSEA)
        self.assertIsNotNone(club)
        self.assertIsNone(self.module.club_identity_score(self.ACADEMY),
                          "发展队/青训学院不能被当作一线队")
        self.assertIsNone(self.module.club_identity_score(self.WOMEN))
        self.assertIsNone(self.module.club_identity_score(self.HISTORY))
        self.assertIsNone(self.module.club_identity_score(self.SHELL),
                          "没有任何可取值字段的实体不应被采用")

    def test_the_senior_club_wins_even_when_it_is_not_the_first_candidate(self):
        """Ranking, not first-match, is what makes this correct."""
        candidates = [("Q2669653", self.ACADEMY), ("Q2422811", self.WOMEN),
                      ("Q9616", self.CHELSEA), ("Q2562544", self.HISTORY)]
        scored = [(position, qid) for position, (qid, entity) in enumerate(candidates)
                  if self.module.club_identity_score(entity) is not None]
        self.assertEqual([qid for _, qid in scored], ["Q9616"])

    def test_an_entity_is_re_checked_when_a_run_resumes(self):
        """A stored answer is not evidence; it is checked again.

        The first pass wrote the academy entity into the plan. A resume that
        trusted its own earlier output would have carried it into the catalogue
        with nothing ever looking at it twice, so acceptance is re-derived from
        the entity's own claims every time.
        """
        with patch.object(self.module, "get", return_value={
                "entities": {"Q2669653": {"claims": self.ACADEMY["claims"],
                                          "descriptions": self.ACADEMY["descriptions"]}}}):
            self.assertFalse(self.module.entity_still_acceptable("Q2669653"))
        with patch.object(self.module, "get", return_value={
                "entities": {"Q9616": {"claims": self.CHELSEA["claims"],
                                       "descriptions": self.CHELSEA["descriptions"]}}}):
            self.assertTrue(self.module.entity_still_acceptable("Q9616"))
        # A lookup that fails must re-queue rather than silently accept.
        with patch.object(self.module, "get", side_effect=RuntimeError("offline")):
            self.assertFalse(self.module.entity_still_acceptable("Q9616"))
        self.assertFalse(self.module.entity_still_acceptable(""))
        self.assertFalse(self.module.entity_still_acceptable(None))
