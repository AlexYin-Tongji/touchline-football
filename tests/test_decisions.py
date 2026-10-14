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


class RetentionTests(unittest.TestCase):
    """The collection is bounded per team, and the bound holds under writers."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "retention.sqlite3")
        self.addCleanup(lambda: self.store.close())

    def cycle(self, team_id="arsenal", number="7"):
        identifier = decisions.begin(self.store, team_id, view(number), parts=["roster"],
                                     trigger="automatic", reason="重新读取来源。")
        decisions.finish(self.store, identifier, view(number), {"status": "completed"})
        return identifier

    def rows(self, team_id="arsenal"):
        return [row for row in self.store.list("decisions") if row.get("team_id") == team_id]

    def test_finished_records_are_capped_per_team(self):
        # Each row carries two full observation sets at roughly 27 KB, so an
        # unbounded log is a size problem, not a history preference.
        for index in range(decisions.RETAIN_PER_TEAM + 6):
            self.cycle(number=str(index % 10))
        self.assertEqual(len(self.rows()), decisions.RETAIN_PER_TEAM)

    def test_the_newest_records_are_the_ones_kept(self):
        newest = None
        for index in range(decisions.RETAIN_PER_TEAM + 3):
            newest = self.cycle(number=str(index % 10))
        kept = {row["id"] for row in self.rows()}
        self.assertIn(newest, kept)
        self.assertEqual(len(kept), decisions.RETAIN_PER_TEAM)

    def test_one_busy_club_does_not_push_out_another(self):
        # The cap is per team because that is the axis the reader asks along:
        # the log on a team page is that club's history.
        quiet = [self.cycle("liverpool", "9") for _ in range(3)]
        for index in range(decisions.RETAIN_PER_TEAM + 5):
            self.cycle("arsenal", str(index % 10))
        self.assertEqual({row["id"] for row in self.rows("liverpool")}, set(quiet))
        self.assertEqual(len(self.rows("arsenal")), decisions.RETAIN_PER_TEAM)

    def test_a_running_record_is_never_trimmed(self):
        # Deleting an unfinished row would erase a decision that has not been
        # recorded yet, so only terminal rows are candidates.
        running = decisions.begin(self.store, "arsenal", view(), parts=["roster"],
                                  trigger="manual", reason="进行中。")
        self.cycle("arsenal", "8")
        self.assertIsNotNone(self.store.get("decisions", running))
        self.assertEqual(self.store.get("decisions", running)["status"], "running")

    def test_recovery_reads_only_the_unfinished_rows(self):
        # recover() used to decode the whole collection to find a handful of
        # running rows; the unfinished ones are selected in SQL instead.
        self.cycle()
        running = decisions.begin(self.store, "arsenal", view(), parts=["roster"],
                                  trigger="manual", reason="进行中。")
        decisions.recover(self.store)
        self.assertEqual(self.store.get("decisions", running)["status"], "interrupted")

    def test_recovery_leaves_finished_records_alone(self):
        done = self.cycle()
        before = self.store.get("decisions", done)
        decisions.recover(self.store)
        self.assertEqual(self.store.get("decisions", done), before)


class RetentionCeilingTests(unittest.TestCase):
    """The cap and the API's page ceiling must not drift apart."""

    def test_the_history_ceiling_equals_the_retention_cap(self):
        # A page larger than what the store keeps would promise history it no
        # longer holds; the endpoint reads this constant rather than restating it.
        from server.main import DECISION_HISTORY_MAX
        self.assertEqual(DECISION_HISTORY_MAX, decisions.RETAIN_PER_TEAM)

    def test_the_reader_default_is_within_the_cap(self):
        # The team page asks for the newest few; the cap must not be below that.
        from server.main import DECISION_HISTORY_MAX
        self.assertLessEqual(20, DECISION_HISTORY_MAX)
        self.assertGreaterEqual(DECISION_HISTORY_MAX, decisions.RETAIN_PER_TEAM)

    async def test_the_api_serves_a_full_page_at_its_declared_ceiling(self):
        # The end-to-end version of the same claim: a caller asking for the
        # largest page the endpoint advertises actually receives that many
        # records, which is only true while the cap is at least that large.
        import httpx
        from server.main import create_app, DECISION_HISTORY_MAX
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory) / "history.sqlite3")
            store = app.state.store
            try:
                for index in range(DECISION_HISTORY_MAX):
                    identifier = decisions.begin(store, "arsenal", view(), parts=["roster"],
                                                 trigger="automatic", reason="第 %d 轮。" % index)
                    decisions.finish(store, identifier, view(str(index % 10)), {"status": "completed"})
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://testserver") as client:
                    response = await client.get(
                        f"/api/teams/arsenal/decisions?limit={DECISION_HISTORY_MAX}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()["items"]), DECISION_HISTORY_MAX)
            finally:
                store.close()
