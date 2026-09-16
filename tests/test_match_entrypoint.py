import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from server.main import create_app


class TeamCenterMatchEntrypointTests(unittest.TestCase):
    def test_league_game_resolves_to_team_view_and_keeps_followup_scope(self):
        with tempfile.TemporaryDirectory() as folder, TestClient(create_app(Path(folder) / "app.db")) as client:
            store = client.app.state.store
            game = {"id": "espn-PL-2026-98765", "league_id": "PL", "season_id": "PL-2026", "season_start_year": 2026,
                    "provider_match_id": "98765", "home_team_id": "arsenal", "away_team_id": "chelsea", "home_team": "Arsenal", "away_team": "Chelsea",
                    "kickoff": "2026-09-17T19:00:00+00:00", "kickoff_date": "2026-09-17", "kickoff_confirmed": True, "status": "scheduled",
                    "home_score": None, "away_score": None, "stadium": "Emirates Stadium", "source_url": "https://www.espn.com/soccer/match/_/gameId/98765", "fetched_at": "2026-09-16T08:00:00+00:00"}
            store.put("league_matches", game)
            payload = {"mode": "live", "team_id": "arsenal", "match_id": game["id"], "question": "本场赛前看什么", "skill": "match_preview", "allow_network": False}
            response = client.post("/api/runs", json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            first = response.json()
            self.assertEqual(first["match_id"], game["id"] + "-arsenal")
            self.assertEqual(store.get("matches", first["match_id"])["team_id"], "arsenal")
            followup = client.post("/api/runs", json={**payload, "question": "还有哪些需要观察", "conversation_id": first["conversation_id"]})
            self.assertEqual(followup.status_code, 200, followup.text)
            self.assertEqual(followup.json()["match_id"], first["match_id"])
            self.assertEqual(client.post("/api/runs", json={**payload, "team_id": "barcelona"}).status_code, 400)
            self.assertEqual(client.post("/api/runs", json={**payload, "mode": "demo"}).status_code, 400)
