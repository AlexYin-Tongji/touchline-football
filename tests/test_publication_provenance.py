"""A source's calendar date remains a date throughout reading and delivery."""
import hashlib
import json
import unittest

from server.agent_tools import LocalTools
from server.agent_delegation import source_version
from server.news_reports import failure_kind, source_version_hash
from server.team_profiles import _evidence_hash
from tests.test_agent import FakeStore


class PublicationProvenanceTests(unittest.TestCase):
    def test_day_only_publication_is_read_and_changed_metadata_invalidates_cache(self):
        store = FakeStore()
        evidence = store.get("evidence", "ev_injury")
        evidence.update(published_at=None, published_date="2026-09-30", publication_precision="date")
        store.put("evidence", evidence)
        local = LocalTools(store, {"id": "run_one", "team_id": "arsenal", "mode": "live", "skill": "news_report"}, store.settings())
        read = local.call("read_evidence", {"evidence_id": "ev_injury"})
        self.assertIsNone(read["published_at"])
        self.assertEqual(read["published_date"], "2026-09-30")
        self.assertEqual(source_version(evidence)["publication_precision"], "date")
        self.assertTrue(local.cache_is_current("ev_injury"))
        before = source_version_hash(evidence)
        evidence["published_date"] = "2026-09-29"
        store.put("evidence", evidence)
        self.assertFalse(local.cache_is_current("ev_injury"))
        self.assertNotEqual(source_version_hash(evidence), before)

    def test_absent_date_fields_do_not_invalidate_older_proofs(self):
        evidence = {"title": "Article", "url": "https://example.com/news", "text": "Original text"}
        expected = {key: evidence.get(key) for key in ("title", "url", "source_name", "published_at", "origin", "spoiler")}
        expected["body_hash"] = hashlib.sha256(evidence["text"].encode()).hexdigest()
        old = hashlib.sha256(json.dumps(expected, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(_evidence_hash(evidence), old)
        self.assertEqual(_evidence_hash({**evidence, "published_date": None, "publication_precision": None}), old)
        precise = {**evidence, "published_at": "2026-09-30T16:00:00+00:00"}
        augmented = {**precise, "published_date": "2026-09-30", "publication_precision": "instant"}
        self.assertEqual(_evidence_hash(precise), _evidence_hash(augmented))
        self.assertEqual(source_version(precise), source_version(augmented))

    def test_quota_exhaustion_does_not_authorize_another_paid_retry(self):
        for error in ("HTTP 429 insufficient_quota", "模型余额不足", "HTTP 429 billing balance exhausted"):
            self.assertEqual(failure_kind({"error": error}), "source_or_configuration")
        self.assertEqual(failure_kind({"error": "HTTP 429 temporary rate limit 限流"}), "transient")
