"""A scripted portrait leaf: the model commands, the tools keep the proof."""
import unittest

from server.portrait_agent import ACTIONS, run_actions
from server.team_portraits import espn_headshot


HEADSHOT = "https://a.espncdn.com/i/headshots/soccer/players/full/241077.png"
WIKIDATA = {
    "status": "completed",
    "photo_url": "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/Ben.jpg/300px-Ben.jpg",
    "photo_credit": "Example",
    "photo_source_url": "https://www.wikidata.org/wiki/Q27839155",
    "photo_file_page": "https://commons.wikimedia.org/wiki/File:Ben.jpg",
    "photo_license": "CC BY 2.0",
    "photo_match": "wikidata_espn_provider_id",
}
CLUB = {
    "status": "completed",
    "photo_url": "https://www.tottenhamhotspur.com/photo-resources/example.jpg",
    "photo_credit": "Tottenham Hotspur 官方球员目录",
    "photo_source_url": "https://www.tottenhamhotspur.com/teams/mens/squad",
    "photo_file_page": "https://www.tottenhamhotspur.com/player/example",
    "photo_license": "俱乐部版权，使用须保留署名",
    "photo_match": "exact_name_and_number",
}


def squad():
    return {"team_id": "arsenal", "roster": [
        {"provider_id": "241077", "name": "Ben White", "photo_url": "",
         "headshot": {"href": HEADSHOT}},
        {"provider_id": "280555", "name": "Bukayo Saka", "photo_url": ""},
        {"provider_id": "189007", "name": "Reece James", "photo_url": "",
         "namesake_blocked": True, "reason_code": "community_ambiguous"},
        {"provider_id": "1", "name": "No Proof", "photo_url": ""},
    ]}


class PortraitAgentSurfaceTests(unittest.IsolatedAsyncioTestCase):
    def tools(self, *, wikidata=None, club=None):
        async def wikidata_p18(player):
            if player["provider_id"] == "280555":
                return {"status": "missing", "reason_code": "wikidata_no_espn_id"}
            return dict(wikidata or WIKIDATA)

        async def wikimedia_club(player):
            return {"status": "missing", "reason_code": "missing_article"}

        async def club_directory(player):
            return dict(club or {})

        return {"wikidata_p18": wikidata_p18, "wikimedia_club": wikimedia_club,
                "club_directory": club_directory}

    async def test_a_helpful_script_attaches_only_proved_faces(self):
        """The leaf the report recommends: read the gap, try a proof, switch, stop.

        Ben White has an ESPN headshot whose path id matches the roster id, so
        the first command attaches it. Saka has no P3681 hit in this fixture,
        so the model opens the club tool, hears that Arsenal has no reviewed
        extractor, tries the id-bound community command, and gives up. That
        command must not turn into a name search. Reece James stays blocked.
        """
        snapshot = squad()
        actions = [
            {"action": "read_gaps"},
            {"action": "espn_headshot", "provider_id": "241077"},
            {"action": "wikidata_p18", "provider_id": "280555"},
            {"action": "club_directory", "provider_id": "280555"},
            {"action": "community_idespn", "provider_id": "280555"},
            {"action": "give_up", "provider_id": "280555", "reason": "no_identity_proof"},
            {"action": "give_up", "provider_id": "189007", "reason": "namesake_blocked"},
            {"action": "wikidata_p18", "provider_id": "1"},
        ]
        result, trace, score = await run_actions(snapshot, actions, self.tools())
        by_id = {row["provider_id"]: row for row in result["roster"]}
        self.assertEqual(by_id["241077"]["photo_match"], "espn_provider_id")
        self.assertEqual(by_id["241077"]["photo_url"], HEADSHOT)
        self.assertFalse(by_id["280555"].get("photo_url"))
        self.assertEqual(by_id["280555"]["reason_code"], "agent_gave_up")
        self.assertTrue(by_id["189007"]["namesake_blocked"])
        self.assertFalse(by_id["189007"].get("photo_url"))
        self.assertEqual(by_id["1"]["photo_match"], "wikidata_espn_provider_id")
        self.assertEqual(score["illegal_attach"], 0)
        self.assertEqual(score["attached_with_proof"], 2)
        self.assertEqual(score["retired_name_search"], 1)
        self.assertGreaterEqual(score["switched_after_miss"], 2)
        self.assertEqual(score["namesake_held"], 1)
        self.assertEqual(score["gave_up"], 2)
        self.assertTrue(any(step.get("reason_code") == "club_site_unconfigured" for step in trace))
        self.assertIn("read_gaps", ACTIONS)

    async def test_the_model_cannot_invent_a_url_or_lift_a_namesake_block(self):
        snapshot = squad()
        actions = [
            {"action": "wikidata_p18", "provider_id": "189007", "photo_url": "https://example.test/face.jpg"},
            {"action": "espn_headshot", "provider_id": "189007", "override_namesake": True},
            {"action": "give_up", "provider_id": "189007", "reason": "the model wrote this sentence"},
            {"action": "search_by_name", "provider_id": "1"},
        ]
        result, trace, score = await run_actions(snapshot, actions, self.tools())
        self.assertEqual(score["refused_invention"], 2)
        self.assertEqual(score["illegal_attach"], 0)
        self.assertEqual(score["attached_with_proof"], 0)
        self.assertTrue(result["roster"][2]["namesake_blocked"])
        self.assertFalse(any(row.get("photo_url") for row in result["roster"]))
        self.assertEqual([step["reason_code"] for step in trace], [
            "model_cannot_supply_url", "namesake_birthdate_refusal",
            "give_up_reason_required", "unknown_action"])

    async def test_a_tool_that_returns_the_wrong_proof_attaches_nothing(self):
        snapshot = {"team_id": "tottenham-hotspur", "roster": [
            {"provider_id": "9", "name": "Example", "photo_url": ""}]}
        sneaky = dict(WIKIDATA, photo_match="community_name_and_birthdate")

        async def wikidata_p18(player):
            return sneaky

        _, trace, score = await run_actions(snapshot, [
            {"action": "wikidata_p18", "provider_id": "9"}],
            {"wikidata_p18": wikidata_p18})
        self.assertFalse(snapshot["roster"][0].get("photo_url"))
        self.assertEqual(score["illegal_attach"], 0)
        self.assertEqual(trace[0]["reason_code"], "no_proof")

    async def test_a_reviewed_club_directory_can_attach_on_its_own_proof(self):
        snapshot = {"team_id": "tottenham-hotspur", "roster": [
            {"provider_id": "9", "name": "Example", "photo_url": ""}]}

        async def club_directory(player):
            return dict(CLUB)

        result, _, score = await run_actions(snapshot, [
            {"action": "club_directory", "provider_id": "9"}],
            {"club_directory": club_directory})
        self.assertEqual(result["roster"][0]["photo_match"], "exact_name_and_number")
        self.assertEqual(score["attached_with_proof"], 1)
        self.assertEqual(score["illegal_attach"], 0)

    def test_espn_headshot_still_refuses_a_manufactured_cdn_path(self):
        photo = espn_headshot({"id": "241077", "headshot": {
            "href": "https://a.espncdn.com/i/headshots/soccer/players/full/999.png"}}, fetched_at="2026-10-14T00:00:00Z")
        self.assertEqual(photo, {})


if __name__ == "__main__":
    unittest.main()
