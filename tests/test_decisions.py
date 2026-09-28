"""Maintenance decisions retain observations without retaining article bodies."""
import json
from pathlib import Path
import tempfile
import unittest

from server import decisions
from server.storage import Store


def view(number="7", fetched="2026-09-28T01:00:00+00:00"):
    return {"season": {"id": "PL-2026", "label": "2026/27"},
            "roster": [{"id": "p", "name": "Player", "number": number,
                        "team_id": "arsenal", "text": "private body", "api_key": "secret"}],
            "roster_updated_at": fetched, "roster_source_updated_at": "2026-09-27T20:00:00Z",
            "matches": [{"id": "m", "opponent_id": "other", "goals_for": 4, "goals_against": 1,
                         "status": "finished", "source_url": "https://www.espn.com/game/1?token=secret"}],
            "standings": [{"team_id": "arsenal", "rank": 2, "points": 30}],
            "components": {"roster": {"updated_at": fetched}, "schedule": {}, "standings": {}},
            "sources": [{"name": "ESPN", "url": "https://www.espn.com/squad/1?api_key=secret"}]}


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "audit.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())

    def begin(self, data=None):
        return decisions.begin(self.store, "arsenal", data or view(), parts=["roster"],
                               trigger="automatic", reason="阵容已超过时效，重新读取来源。")

    def test_changed_squad_retains_both_observations_and_two_clocks(self):
        identifier = self.begin()
        decisions.finish(self.store, identifier, view("10", "2026-09-28T02:00:00Z"),
                         {"status": "completed", "components": {"roster": {"status": "completed"}}})
        row = self.store.get("decisions", identifier)
        self.assertEqual(row["before"]["roster"]["values"][0]["number"], "7")
        self.assertEqual(row["after"]["roster"]["values"][0]["number"], "10")
        self.assertEqual(row["after"]["roster"]["observed_at"], "2026-09-27T20:00:00Z")
        self.assertEqual(row["changes"][0]["field"], "roster")
        self.assertTrue(row["changes"][0]["changed"])
        self.assertTrue(row["changes"][0]["confirmed"])

    def test_failed_refresh_does_not_claim_a_new_confirmation(self):
        first = self.begin()
        decisions.finish(self.store, first, view(), {"status": "completed"})
        second = self.begin()
        decisions.finish(self.store, second, view(), {"status": "failed", "errors": ["api_key=secret"]})
        row = self.store.get("decisions", second)
        self.assertEqual(row["previous_id"], first)
        self.assertFalse(row["changes"][0]["confirmed"])
        self.assertEqual(row["after"]["roster"]["fetched_at"], "2026-09-28T01:00:00+00:00")
        self.assertNotIn("secret", json.dumps(row))

    def test_only_structured_fields_and_safe_source_urls_are_retained(self):
        identifier = self.begin()
        raw = json.dumps(self.store.get("decisions", identifier))
        self.assertNotIn("private body", raw)
        self.assertNotIn("secret", raw)
        self.assertNotIn("api_key", raw)
        self.assertIn("espn.com", raw)

    def test_spoiler_setting_protects_history_even_after_setting_changes(self):
        self.store.save_settings({"spoiler_free": False})
        identifier = decisions.begin(self.store, "arsenal", view(), parts=["schedule", "standings"],
                                     trigger="manual", reason="更新比赛资料。")
        decisions.finish(self.store, identifier, view(), {"status": "completed"})
        self.store.save_settings({"spoiler_free": True})
        row = decisions.history(self.store, "arsenal")["items"][0]
        self.assertNotIn("values", row["before"]["schedule"])
        self.assertNotIn("fingerprint", row["after"]["standings"])
        self.assertTrue(row["spoiler_hidden"])

    def test_recovery_survives_restart_and_marks_unfinished_work(self):
        identifier = self.begin()
        self.store.close()
        self.store = Store(self.path)
        decisions.recover(self.store)
        row = decisions.history(self.store, "arsenal")["items"][0]
        self.assertEqual(row["id"], identifier)
        self.assertEqual(row["status"], "interrupted")
        self.assertNotIn("after", row)

    def test_history_is_team_scoped_and_bounded(self):
        first = self.begin()
        second = self.begin()
        decisions.begin(self.store, "barcelona", view(), parts=["roster"], trigger="manual", reason="更新阵容。")
        items = decisions.history(self.store, "arsenal", limit=1)["items"]
        self.assertEqual([row["id"] for row in items], [second])
        self.assertNotEqual(first, second)

    def test_photo_source_and_replacement_are_part_specific(self):
        before = view()
        before["roster"][0].update(photo_url="https://upload.wikimedia.org/old.jpg?token=secret",
            photo_source_url="https://commons.wikimedia.org/wiki/File:Old.jpg?token=secret")
        before["part_sources"] = {"roster": [{"url": "https://www.espn.com/squad/1"}],
            "standings": [{"url": "https://www.espn.com/standings/1"}]}
        identifier = decisions.begin(self.store, "arsenal", before, parts=["portraits"], trigger="manual", reason="照片待更新。")
        after = view()
        after["roster"][0].update(photo_url="https://upload.wikimedia.org/new.jpg",
            photo_source_url="https://commons.wikimedia.org/wiki/File:New.jpg")
        decisions.finish(self.store, identifier, after, {"status": "completed",
            "components": {"portraits": {"status": "completed", "cached": False}}})
        row = self.store.get("decisions", identifier)
        self.assertTrue(row["changes"][0]["changed"])
        self.assertIn("commons.wikimedia.org", row["after"]["portraits"]["sources"][0]["url"])
        self.assertNotIn("espn.com", json.dumps(row["after"]["portraits"]["sources"]))
        self.assertNotIn("secret", json.dumps(row))

    def test_cached_snapshot_does_not_claim_confirmation(self):
        identifier = self.begin()
        decisions.finish(self.store, identifier, view(), {"status": "completed",
            "components": {"roster": {"status": "completed", "cached": True}}})
        row = self.store.get("decisions", identifier)
        self.assertFalse(row["changes"][0]["confirmed"])


if __name__ == "__main__":
    unittest.main()
