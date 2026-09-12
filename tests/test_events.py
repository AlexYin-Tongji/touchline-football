import tempfile
import unittest
from pathlib import Path

from server.events import suggest_stage
from server.ingestion import ingest
from server.seed import seed
from server.storage import Store


class EventTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "events.db")
        seed(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def add(self, **patch):
        return ingest(self.store, **{"team_id": "arsenal", "title": "Saka injury fitness update", "text": "Saka has an injury. The club will assess his fitness before the next match.", "published_at": "2026-09-08T12:00:00Z", "url": "https://www.arsenal.com/news/saka-fitness", **patch})

    def test_duplicate_does_not_create_stage_change_or_second_confirmation(self):
        first = self.add(source_name="Original club statement")
        second = self.add(source_name="A repost", text="  " + first["evidence"]["text"] + "\n")
        self.assertTrue(second["duplicate"])
        event = second["event"]
        self.assertEqual(len(event["versions"]), 1)
        self.assertEqual(len(event["timeline"]), 1)
        self.assertEqual(event["source_names"], ["Original club statement"])

    def test_body_revision_keeps_both_versions_and_source_claims(self):
        first = self.add()
        next_item = self.add(text="Saka has an injury but the coach now says he is back in full training. No team selection has been announced.", published_at="2026-09-09T12:00:00Z")
        self.assertEqual(first["event"]["id"], next_item["event"]["id"])
        self.assertEqual(len(next_item["event"]["versions"]), 2)
        self.assertIn("恢复训练", next_item["event"]["stage_suggestion"]["label"])
        self.assertFalse(next_item["event"]["stage_suggestion"]["verified"])

    def test_negated_and_speculative_claims_never_mark_return_confirmed(self):
        for text in ("Saka has an injury. He has not returned to full training.", "Saka injury update: He could return to full training tomorrow."):
            self.assertNotIn("恢复训练", suggest_stage("Saka injury update", text)["label"])

    def test_older_source_is_preserved_without_overwriting_latest_stage(self):
        first = self.add(text="Saka has an injury but is back in full training. The coach has not announced the final squad.")
        old = self.add(text="Saka has an injury and will miss the match against Napoli. This statement was published earlier.", published_at="2026-09-01T12:00:00Z")
        self.assertEqual(old["event"]["stage"], first["event"]["stage"])
        self.assertEqual(len(old["event"]["versions"]), 2)
        self.assertIn("补录", old["event"]["change"])

    def test_conservative_grouping_requires_same_person_topic_and_close_date(self):
        first = self.add()
        same = self.add(url="https://www.arsenal.com/news/saka-latest", title="Saka injury fitness update today", text="Saka's injury fitness will be assessed after training. The club will provide another update.")
        self.assertEqual(first["event"]["id"], same["event"]["id"])
        other_person = self.add(url="https://www.arsenal.com/news/rice", title="Rice injury fitness update", text="Rice has an injury and his fitness will be assessed before the next match.")
        old_event = self.add(url="https://www.arsenal.com/news/old-saka", text="Saka injury update from an earlier season. His fitness will be assessed again before the fixture.", published_at="2025-08-08T12:00:00Z")
        self.assertNotEqual(first["event"]["id"], other_person["event"]["id"])
        self.assertNotEqual(first["event"]["id"], old_event["event"]["id"])

    def test_same_url_different_team_does_not_join_and_explicit_stage_survives(self):
        first = self.add(stage="用户确认的记录阶段")
        other = self.add(team_id="liverpool")
        later = self.add(text="Saka has an injury and remains under assessment; the club has not announced the final squad.")
        self.assertNotEqual(first["event"]["id"], other["event"]["id"])
        self.assertEqual(later["event"]["stage"], "用户确认的记录阶段")


if __name__ == "__main__":
    unittest.main()
