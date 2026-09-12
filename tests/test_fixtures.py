import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server import fixtures, ingestion
from server.storage import Store


def arsenal_payload(*, kickoff="2026-09-06T15:30", explicit="15:30:00Z", state="postmatch", scope="men", score=(2, 1)):
    detail = {"scoreArsenal": score[0], "scoreOpponent": score[1], "venueType1": "home", "venue": "Emirates Stadium",
              "fixtureDate": kickoff, "dateOverride": kickoff, "matchStatus": {"label": "Full time" if state == "postmatch" else state},
              "arsenalTeam": {"slug": scope, "name": "Arsenal"}, "opponent": {"name": "Chelsea", "clubName": "Chelsea"}}
    editor = {"id": 11411, "matchState": state, "customTypeData": {"fixtureConfig": {"optaId": "fixture1"}, "fixtureDetails": detail},
              "seo": {"slug": "arsenal-chelsea"}}
    match = {"matchInfo": {"id": "fixture1", "date": kickoff[:10] + "Z", "time": explicit,
                           "lastUpdated": "2026-09-07T00:00:00Z", "contestant": [{"id": fixtures.ARSENAL_TEAM_ID}],
                           "competition": {"name": "Premier League"}},
             "liveData": {"matchDetails": {"matchStatus": "Played", "scores": {"total": {"home": score[0], "away": score[1]}}}}}
    return {"data": {"getLiveReportsListByFilters": {"data": [editor]}, "fixturesByIds": {"matches": [match]}}}


def inter_page(status="Ended", confirmed=True, team_id="3vo5mpj7catp66nrwwqiuhuup", kickoff="2026-09-08T21:00:00+02:00"):
    match = {"id": "5281", "teams": {"homeTeam": {"id": "madrid", "name": "Real Madrid"},
                                        "awayTeam": {"id": team_id, "name": "Inter"}}, "date": kickoff,
             "isDateConfirmed": confirmed, "competition": {"name": "UEFA Champions League"},
             "score": {"homeTeam": 2, "awayTeam": 1}, "status": status}
    payload = {"query": {"teams": "men"}, "props": {"pageProps": {"matchesPageData": {"allMatchesSeasonData": [[match]], "activeSeasonName": "2026/27"}}}}
    return ('<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + '</script>').encode()


def flight_page(*, tbc=False, home="Liverpool", away="Fulham", scores=None):
    children = [{"kickOffUtc": "2026-09-12T15:00:00+01:00", "dateTbc": tbc, "timeTbc": tbc},
                {"className": "fixtureCard__teamName__a", "children": home},
                {"className": "fixtureCard__teamName__a", "children": away}]
    children += [{"className": "fixtureCard__score__a", "children": score} for score in scores or []]
    card = {"data-testid": "fixture-card-slotted", "children": children}
    script = "self.__next_f.push(" + json.dumps([1, "1:" + json.dumps(card)]) + ")"
    html = f'<script>{script}</script><article data-testid="fixture-card-slotted"><span class="fixtureCard__teamName__a">{home}</span><span class="fixtureCard__teamName__a">{away}</span><div class="fixtureCard__competitionInline__a">Premier League</div><a href="/matches/mens-team/test-fixture">Match Centre</a></article>'
    return html.encode()


class FixtureParsingTests(unittest.TestCase):
    def test_arsenal_utc_needs_two_matching_fields(self):
        row = fixtures.parse_arsenal(arsenal_payload())[0]
        self.assertEqual(row["kickoff"], "2026-09-06T15:30:00+00:00")
        self.assertTrue(row["kickoff_confirmed"])
        # Summer London offset must not be applied again to already UTC data.
        row = fixtures.parse_arsenal(arsenal_payload(explicit="14:30:00Z"))[0]
        self.assertFalse(row["kickoff_confirmed"])
        self.assertEqual(row["kickoff"], "")
        self.assertEqual(row["kickoff_date"], "2026-09-06")

    def test_winter_utc_does_not_shift_dates(self):
        row = fixtures.parse_arsenal(arsenal_payload(kickoff="2026-12-06T15:30"))[0]
        self.assertEqual(row["kickoff"], "2026-12-06T15:30:00+00:00")

    def test_arsenal_excludes_same_named_womens_team(self):
        self.assertEqual(fixtures.parse_arsenal(arsenal_payload(scope="women")), [])
        self.assertEqual(fixtures.parse_arsenal(arsenal_payload(scope="mens-academy-u18s")), [])

    def test_conflicting_official_score_is_withheld(self):
        data = arsenal_payload()
        data["data"]["fixturesByIds"]["matches"][0]["liveData"]["matchDetails"]["scores"]["total"]["home"] = 3
        row = fixtures.parse_arsenal(data)[0]
        self.assertIsNone(row["result"])
        self.assertIsNone(row["score"])
        self.assertTrue(any("冲突" in warning for warning in row["data_warnings"]))

    def test_away_score_orientation_and_positive_timezone(self):
        row = fixtures.parse_inter(inter_page(), fixtures.SOURCES["inter"]["url"])[0]
        self.assertEqual(row["kickoff"], "2026-09-08T19:00:00+00:00")
        self.assertEqual((row["score"], row["goals_for"], row["goals_against"], row["result"]), ("1–2", 1, 2, "L"))
        self.assertEqual((row["home_score"], row["away_score"]), (2, 1))

    def test_postponed_and_cancelled_never_become_results(self):
        for status in ("Postponed", "Cancelled", "Abandoned"):
            row = fixtures.parse_inter(inter_page(status=status), fixtures.SOURCES["inter"]["url"])[0]
            self.assertEqual(row["status"], "scheduled")
            self.assertIsNone(row["score"])
            self.assertFalse(row["kickoff_confirmed"])
            self.assertIn(row["fixture_status"], {"postponed", "cancelled"})

    def test_mens_scope_is_checked_against_club_team_id(self):
        self.assertEqual(fixtures.parse_inter(inter_page(team_id="women"), fixtures.SOURCES["inter"]["url"]), [])

    def test_unconfirmed_liverpool_time_stays_unknown(self):
        row = fixtures.parse_liverpool(flight_page(tbc=True), fixtures.SOURCES["liverpool"]["url"])[0]
        self.assertEqual(row["kickoff"], "")
        self.assertFalse(row["kickoff_confirmed"])
        row = fixtures.parse_liverpool(flight_page(), fixtures.SOURCES["liverpool"]["url"])[0]
        self.assertEqual(row["kickoff"], "2026-09-12T14:00:00+00:00")

    def test_liverpool_does_not_infer_finished_from_old_date(self):
        row = fixtures.parse_liverpool(flight_page(scores=[2, 1]), fixtures.SOURCES["liverpool"]["url"])[0]
        self.assertEqual(row["status"], "scheduled")
        self.assertIsNone(row["result"])

    def test_barcelona_provisional_midnight_is_not_kickoff(self):
        body = b'''<li class="fixture-result-list__fixture"><a class="fixture-result-list__link" href="/en/matches/123/barcelona-getafe"><div class="fixture-result-list__tbc-explainer">Date and time to be announced</div><div data-fixture-date="1791730800000"></div><div class="fixture-info__name--home">FC Barcelona</div><div class="fixture-info__name--away">Getafe</div></a></li>'''
        row = fixtures.parse_barcelona(body, fixtures.SOURCES["barcelona"]["url"])[0]
        self.assertEqual(row["kickoff"], "")
        self.assertFalse(row["kickoff_confirmed"])
        self.assertIsNone(row["score"])

    def test_arsenal_lineups_require_same_match_time_and_exact_eleven(self):
        match = fixtures.parse_arsenal(arsenal_payload())[0]
        names = ", ".join("Player " + str(i) for i in range(11))
        article = {"url": "https://www.arsenal.com/news/team-news-chelsea", "published_at": "2026-09-06T14:15:00Z",
                   "text": "LINE-UPS\nArsenal: " + names + ".\nSubs: Bench player\nChelsea: " + names + "."}
        self.assertEqual(fixtures.parse_arsenal_lineups(article, match)["status"], "confirmed")
        article["published_at"] = "2025-09-06T14:15:00Z"
        self.assertEqual(fixtures.parse_arsenal_lineups(article, match)["status"], "unavailable")

    def test_result_fixture_record_cannot_be_paragraph_redacted(self):
        item = {"fixture_record": True, "spoiler": True, "origin": "web", "text": '"home_score": 3\n"away_score": 1', "title": "比赛资料"}
        self.assertIsNone(ingestion.safe_evidence_view(item))
        item["spoiler"] = False
        self.assertEqual(ingestion.safe_evidence_view(item), item)


class FixtureStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "fixtures.db")
        for team_id in fixtures.SOURCES:
            self.store.put("teams", {"id": team_id, "name": team_id})

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def rows(self):
        return fixtures.parse_arsenal(arsenal_payload())

    def test_duplicate_import_does_not_duplicate_matches_or_evidence(self):
        first = fixtures.save_matches(self.store, "arsenal", self.rows(), "2026-09-08T01:00:00+00:00")
        second = fixtures.save_matches(self.store, "arsenal", self.rows() * 2, "2026-09-08T01:30:00+00:00")
        self.assertEqual(first, (1, 1, 0))
        self.assertEqual(second, (0, 0, 0))
        self.assertEqual(len(self.store.list("matches", "live", "arsenal")), 1)
        self.assertEqual(len(self.store.list("evidence", "live", "arsenal")), 1)
        evidence = self.store.list("evidence", "live", "arsenal")[0]
        self.assertTrue(evidence["fixture_record"])
        self.assertIsNone(evidence["published_at"])
        self.assertTrue(evidence["source_updated_at"])

    def test_older_source_cannot_overwrite_latest_result(self):
        rows = self.rows()
        fixtures.save_matches(self.store, "arsenal", rows, "2026-09-08T01:00:00+00:00")
        old = self.rows()
        old[0].update({"source_updated_at": "2026-09-06T00:00:00+00:00", "score": "1–1"})
        self.assertEqual(fixtures.save_matches(self.store, "arsenal", old, "2026-09-08T02:00:00+00:00"), (0, 0, 0))
        self.assertEqual(self.store.get("matches", rows[0]["id"])["score"], "2–1")

    def test_stale_schedule_never_regresses_final_result(self):
        rows = self.rows()
        fixtures.save_matches(self.store, "arsenal", rows, "2026-09-08T01:00:00+00:00")
        old = self.rows()
        old[0].update({"source_updated_at": "", "status": "scheduled", "score": None})
        fixtures.save_matches(self.store, "arsenal", old, "2026-09-08T02:00:00+00:00")
        self.assertEqual(self.store.get("matches", rows[0]["id"])["score"], "2–1")

    def test_future_default_zero_score_does_not_reach_model_evidence(self):
        rows = fixtures.parse_arsenal(arsenal_payload(state="prematch", score=(0, 0)))
        fixtures.save_matches(self.store, "arsenal", rows, "2026-09-01T00:00:00+00:00")
        row = self.store.list("matches", "live", "arsenal")[0]
        self.assertIsNone(row["score"])
        self.assertIsNone(row["result"])
        self.assertIsNone(row["goals_for"])
        raw = row["source_raw"]["editorial"]["customTypeData"]["fixtureDetails"]
        self.assertEqual(raw["scoreArsenal"], 0)
        evidence = self.store.list("evidence", "live", "arsenal")[0]
        self.assertNotIn('"scoreArsenal"', evidence["text"])
        self.assertNotIn('"total"', evidence["text"])
        self.assertIn("不能据此推断赛果", evidence["text"])

    def test_update_preserves_notes_associations_and_versions(self):
        rows = self.rows()
        fixtures.save_matches(self.store, "arsenal", rows, "2026-09-08T01:00:00+00:00")
        saved = self.store.get("matches", rows[0]["id"])
        saved.update({"note": "Watch the midfield", "custom_user_field": True})
        saved["evidence_ids"].append("manual_evidence")
        self.store.put("matches", saved)
        new = self.rows()
        new[0]["kickoff"] = "2026-09-06T16:30:00+00:00"
        fixtures.save_matches(self.store, "arsenal", new, "2026-09-08T02:00:00+00:00")
        updated = self.store.get("matches", rows[0]["id"])
        self.assertEqual(updated["note"], "Watch the midfield")
        self.assertIn("manual_evidence", updated["evidence_ids"])
        self.assertTrue(updated["custom_user_field"])
        self.assertEqual(len(updated["source_versions"]), 2)
        self.assertEqual(updated["source_versions"][-1]["changes"]["kickoff"]["before"], "2026-09-06T15:30:00+00:00")

    async def test_cache_avoids_network_and_force_bypasses(self):
        collect = AsyncMock(return_value=(self.rows(), [], [{"url": "https://www.arsenal.com/fixtures", "status": "ok"}]))
        with patch("server.fixtures._collect", collect):
            await fixtures.sync_team(self.store, "arsenal")
            cached = await fixtures.sync_team(self.store, "arsenal")
            self.assertTrue(cached["cached"])
            self.assertEqual(collect.await_count, 1)
            await fixtures.sync_team(self.store, "arsenal", force=True)
            self.assertEqual(collect.await_count, 2)

    async def test_partial_failure_preserves_available_sources(self):
        async def fetch(team_id, url):
            if "category=results" in url or "/matches/20" in url:
                raise ingestion.IngestionError("Official archive temporarily unavailable")
            return inter_page(), url, "text/html"
        with patch("server.fixtures._fetch_page", side_effect=fetch):
            result = await fixtures.sync_team(self.store, "inter", force=True)
        self.assertEqual(result["matches_updated"], 1)
        self.assertTrue(result["errors"])
        self.assertEqual(len(self.store.list("matches", "live", "inter")), 1)
        self.assertTrue(fixtures.get_team_status(self.store, "inter")["errors"])

    async def test_no_adapter_is_explicit_not_success(self):
        self.store.put("teams", {"id": "unknown", "name": "Unknown"})
        result = await fixtures.sync_team(self.store, "unknown")
        self.assertEqual(result["matches_updated"], 0)
        self.assertTrue(result["errors"])

    async def test_fixed_page_adapter_rejects_outside_host_before_network(self):
        with patch("server.research._fetch", new_callable=AsyncMock) as network:
            with self.assertRaises(ingestion.IngestionError):
                await fixtures._fetch_page("arsenal", "https://other.example/fixtures")
            network.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
