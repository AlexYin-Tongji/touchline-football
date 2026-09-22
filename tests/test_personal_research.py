"""Personal-mode retention, source reconstruction and discovery quality contracts."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server import ingestion, news_reports, research
from server.agent_tools import LocalTools, ToolError
from server.seed import seed
from server.storage import Store
from server.team_profiles import _evidence_hash, knowledge_for_team


BODY = ("The club confirmed that training resumes on Thursday. The medical team will assess the players before the next match. " * 7).strip()
SOURCE = {"id": "source_public", "mode": "live", "team_id": "arsenal", "name": "Club source", "url": "https://example.com/news", "kind": "html", "enabled": True}


class PersonalRetentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "personal.sqlite"
        self.store = Store(self.path)
        seed(self.store)
        self.store.put("sources", SOURCE)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def source(self):
        return ingestion.ingest(self.store, team_id="arsenal", title="Training resumes on Thursday", text=BODY,
                                url="https://example.com/news/training", source_name=SOURCE["name"], source_id=SOURCE["id"],
                                origin="web", published_at="2026-09-16T08:00:00Z", content_status="full_text")

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)

    def test_external_body_only_lives_in_memory_but_manual_note_survives(self):
        source = self.source()["evidence"]
        note = ingestion.ingest(self.store, team_id="arsenal", title="我的观察", text="用户手写的球队观察必须保留。", origin="manual")["evidence"]
        stored = json.loads(self.store._db.execute("SELECT body FROM documents WHERE collection='evidence' AND id=?", (source["id"],)).fetchone()[0])
        self.assertNotIn("text", stored)
        self.assertEqual(stored["body_hash"], hashlib.sha256(BODY.encode()).hexdigest())
        self.assertEqual(self.store.get("evidence", source["id"])["text"], BODY)
        self.reopen()
        self.assertEqual(self.store.get("evidence", source["id"])["text"], "")
        self.assertEqual(self.store.get("evidence", note["id"])["text"], "用户手写的球队观察必须保留。")

    def test_memory_cache_has_expiry_and_byte_budget(self):
        source = self.source()["evidence"]
        with patch("server.storage.time.monotonic", return_value=10 ** 12):
            self.assertFalse(self.store.get("evidence", source["id"])["body_available"])
        self.store.BODY_CACHE_BYTES = 10
        self.store.put("evidence", source)
        self.assertEqual(len(self.store._bodies), 0)

    async def test_agent_rereads_stored_link_after_restart_and_never_quotes_empty_body(self):
        item = self.source()["evidence"]
        self.reopen()
        local = LocalTools(self.store, {"id": "read", "mode": "live", "team_id": "arsenal", "skill": "ask"}, self.store.settings())
        with self.assertRaisesRegex(ToolError, "链接"):
            local.call("read_evidence", {"evidence_id": item["id"]})
        html = f'<html><title>{item["title"]}</title><article><p>{BODY}</p></article></html>'.encode()
        with patch("server.research._fetch", new=AsyncMock(return_value=(html, item["url"], "text/html"))) as fetch:
            read = await local.acall("read_evidence", {"evidence_id": item["id"]})
        self.assertIn("medical team", read["text"])
        self.assertIn(item["id"], local.read_ids)
        self.assertEqual(fetch.await_count, 1)
        self.assertTrue(local.cache_is_current(item["id"]))

    async def test_disabled_source_cannot_be_reread(self):
        item = self.source()["evidence"]
        self.store.put("sources", {**SOURCE, "enabled": False})
        self.store._bodies.clear()
        with patch("server.research._fetch", new_callable=AsyncMock) as fetch:
            with self.assertRaisesRegex(ingestion.IngestionError, "停用"):
                await research.read_evidence_source(self.store, item["id"])
            fetch.assert_not_awaited()

    def test_completed_chinese_report_survives_restart_but_body_correction_invalidates(self):
        imported = self.source()
        event = imported["event"]
        snapshot = news_reports._snapshot(self.store, event, self.store.settings())
        row = {"id": "report_keep", "mode": "live", "team_id": "arsenal", "event_id": event["id"],
               "fingerprint": snapshot["fingerprint"], "status": "completed", "report": {"title": "球队周四恢复训练", "lead": "训练计划已经发布。"}}
        self.store.put("reports", row)
        self.reopen()
        self.assertEqual(news_reports.get_report(self.store, row["id"])["status"], "completed")
        source = self.store.get("evidence", imported["evidence"]["id"])
        source["fetched_at"] = "2026-09-20T10:00:00Z"
        self.store.put("evidence", source)
        self.assertEqual(news_reports.get_report(self.store, row["id"])["status"], "completed")
        source["text"] = "The training session has been cancelled."
        self.store.put("evidence", source)
        self.assertEqual(news_reports.get_report(self.store, row["id"])["status"], "stale")

    def test_upgrade_migrates_only_valid_legacy_report_and_knowledge_provenance(self):
        imported = self.source()
        source, event = imported["evidence"], imported["event"]
        source.pop("body_hash", None)
        source.pop("body_storage", None)
        # Simulate the preceding version's actual persisted publisher body.
        self.store._db.execute("UPDATE documents SET body=? WHERE collection='evidence' AND id=?", (json.dumps(source), source["id"]))
        self.store._db.execute("DELETE FROM metadata WHERE key='transient_bodies_v1'")
        self.store._db.commit()
        old = news_reports._snapshot(self.store, event, self.store.settings(), legacy=True)
        fact = {"title": "训练消息", "detail": "球队将在周四恢复训练，医疗团队将在下一场比赛前评估球员。", "evidence_ids": [source["id"]]}
        content = {"title": "旧中文稿保留", "summary": "正文已整理。", "facts": [fact], "analysis": "", "unknowns": []}
        base = {"id": "legacy_report", "mode": "live", "team_id": "arsenal", "event_id": event["id"], "fingerprint": old["fingerprint"],
                "source_hashes": old["source_hashes"], "status": "completed", "evidence_ids": [source["id"]], "run_id": "verified_legacy_run",
                "report": {"title": content["title"], "lead": content["summary"], "analysis": "", "sections": [{"heading": fact["title"], "body": fact["detail"], "evidence_ids": fact["evidence_ids"]}]}}
        self.store.put("runs", {"id": "verified_legacy_run", "team_id": "arsenal", "mode": "live", "skill": "news_report", "status": "completed",
                                "report_id": "legacy_report",
                                "structured_answer": content, "evidence_ids": [source["id"]], "read_evidence_ids": [source["id"]], "verification": {"status": "supported"}})
        self.store.put("reports", base)
        self.store.put("reports", {**base, "id": "stale_report", "fingerprint": "already-invalid"})
        self.store.put("knowledge", {"id": "knowledge", "team_id": "arsenal", "mode": "live", "title": "训练消息", "detail": "周四训练。",
                                     "evidence_ids": [source["id"]], "sources": [{"id": source["id"], "content_hash": _evidence_hash(source, legacy=True)}]})
        self.reopen()
        self.assertEqual(news_reports.get_report(self.store, "legacy_report")["status"], "completed")
        self.assertEqual(news_reports.get_report(self.store, "stale_report")["status"], "stale")
        self.assertEqual(len(knowledge_for_team(self.store, "arsenal")), 1)
        raw = self.store._db.execute("SELECT body FROM documents WHERE collection='evidence' AND id=?", (source["id"],)).fetchone()[0]
        self.assertNotIn(BODY, raw)

    def test_generic_secret_is_encrypted_and_clearable(self):
        self.store.set_secret("model_profile:a", "private-provider-key")
        self.assertEqual(self.store.get_secret("model_profile:a"), "private-provider-key")
        self.assertNotIn(b"private-provider-key", self.path.read_bytes())
        self.store.set_secret("model_profile:a", None)
        self.assertEqual(self.store.get_secret("model_profile:a"), "")

    def test_article_index_never_persists_fallback_article_body(self):
        entry = {"id": "candidate_body", "title": "News", "text": BODY, "description": BODY}
        self.store.put("candidates", entry)
        self.store.set_meta("news_index:arsenal:test", {"entries": [entry]})
        self.assertNotIn("text", self.store.get("candidates", entry["id"]))
        self.assertNotIn("text", self.store.get_meta("news_index:arsenal:test")["entries"][0])
        self.assertLessEqual(len(self.store.get_meta("news_index:arsenal:test")["entries"][0]["description"]), 280)

    def test_team_data_tool_reads_real_structured_snapshot_with_citable_id(self):
        local = LocalTools(self.store, {"id": "team", "mode": "live", "team_id": "arsenal", "skill": "ask"}, self.store.settings())
        payload = {"season": {"label": "2026/27"}, "roster": [{"name": "Player", "position": "中场"}], "stats": {},
                   "sources": [{"name": "ESPN", "url": "https://www.espn.com/soccer/team/arsenal"}], "updated_at": "2026-09-16T00:00:00Z"}
        with patch("server.team_center.team_context_payload", return_value=payload):
            output = local.call("get_team_data", {})
        evidence = output["evidence"]
        self.assertIn(evidence["id"], local.read_ids)
        self.assertTrue(evidence["passages"])
        self.assertTrue(self.store.get("evidence", evidence["id"])["fixture_record"])
        self.assertNotIn("官网", evidence["source_name"])

    def test_empty_team_data_does_not_manufacture_citable_zero_statistics(self):
        local = LocalTools(self.store, {"id": "team", "mode": "live", "team_id": "arsenal", "skill": "ask"}, self.store.settings())
        with patch("server.team_center.team_context_payload", return_value={"roster": [], "stats": {"season": {"played": 0}}}):
            output = local.call("get_team_data", {})
        self.assertIsNone(output["evidence"])
        self.assertFalse(local.read_ids)

    def legacy_completed(self):
        imported = self.source()
        legacy = imported["evidence"]
        fact = {"title": "训练安排", "detail": "球队将在周四恢复训练，下一场比赛前医疗团队将评估球员。", "evidence_ids": [legacy["id"]]}
        content = {"title": "球队周四恢复训练", "summary": "俱乐部已公布训练安排。", "facts": [fact], "analysis": "", "unknowns": []}
        row = {"id": "legacy_for_proof", "event_id": imported["event"]["id"], "team_id": "arsenal", "mode": "live", "status": "completed",
               "fingerprint": "old-preference-fingerprint", "source_hashes": {legacy["id"]: news_reports._hash(legacy)},
               "evidence_ids": [legacy["id"]], "run_id": "proof_run", "report": {"title": content["title"], "lead": content["summary"], "analysis": "",
                    "sections": [{"heading": fact["title"], "body": fact["detail"], "evidence_ids": fact["evidence_ids"]}]}}
        self.store.put("reports", row)
        self.store.put("runs", {"id": "proof_run", "status": "completed", "mode": "live", "team_id": "arsenal", "skill": "news_report", "report_id": row["id"],
                                "evidence_ids": [legacy["id"]], "read_evidence_ids": [legacy["id"]], "structured_answer": content, "verification": {"status": "supported"}})
        self.store._bodies.clear()
        return row, legacy

    def test_explicit_backup_proof_restores_only_same_source_and_verified_read_delivery(self):
        row, legacy = self.legacy_completed()
        self.store.save_settings({"personalization": {"interests": ["战术"]}, "explanation_depth": "brief"})
        self.assertEqual(news_reports.migrate_report_boundaries(self.store)["preserved"], 0)
        news_reports.register_legacy_source_proof(self.store, row["id"], legacy)
        self.assertEqual(news_reports.migrate_report_boundaries(self.store)["preserved"], 1)
        self.assertEqual(news_reports.get_report(self.store, row["id"])["status"], "completed")
        self.assertEqual(news_reports.enqueue_reports(self.store, force=True)[0]["id"], row["id"])
        proof = self.store.get_meta("legacy_source_provenance_v1")
        self.assertNotIn(BODY, json.dumps(proof))

    def test_new_provenance_is_backed_up_before_write_and_duplicate_is_idempotent(self):
        import sqlite3
        row, legacy = self.legacy_completed()
        with patch.object(self.store, "backup", wraps=self.store.backup) as backup:
            with self.assertRaises(ValueError):
                news_reports.register_legacy_source_proof(self.store, row["id"], {**legacy, "text": "changed"})
            backup.assert_not_called()
            news_reports.register_legacy_source_proof(self.store, row["id"], legacy)
            news_reports.register_legacy_source_proof(self.store, row["id"], legacy)
            backup.assert_called_once_with("before-provenance")
            current = self.store.get("evidence", legacy["id"])
            self.store.put("evidence", {**current, "private": True})
            with self.assertRaises(ValueError):
                news_reports.register_legacy_source_proof(self.store, row["id"], legacy)
            self.assertEqual(backup.call_count, 1)
        saved = next((self.path.parent / "backups").glob("*-before-provenance-*.sqlite3"))
        self.assertEqual(saved.with_suffix(".key").read_bytes(), self.path.with_suffix(".key").read_bytes())
        connection = sqlite3.connect(saved)
        try:
            previous = connection.execute("SELECT body FROM metadata WHERE key='legacy_source_provenance_v1'").fetchone()
            self.assertNotIn(legacy["id"], json.loads(previous[0]) if previous else {})
        finally:
            connection.close()

    def test_legacy_proof_rejects_changed_body_private_source_and_changed_event_membership(self):
        row, legacy = self.legacy_completed()
        with self.assertRaises(ValueError):
            news_reports.register_legacy_source_proof(self.store, row["id"], {**legacy, "text": "changed"})
        original = self.store.get("evidence", legacy["id"])
        for change in ({"text": "Corrected source."}, {"private": True}, {"spoiler": True}):
            self.store.put("evidence", {**original, **change})
            with self.assertRaises(ValueError):
                news_reports.register_legacy_source_proof(self.store, row["id"], legacy)
            self.store.put("evidence", original)
        event = self.store.get("events", row["event_id"])
        self.store.put("events", {**event, "evidence_ids": [*event["evidence_ids"], "deleted-source"]})
        with self.assertRaises(ValueError):
            news_reports.register_legacy_source_proof(self.store, row["id"], legacy)

    def test_legacy_migration_keeps_stale_and_rejects_unread_or_newly_hidden_delivery(self):
        row, legacy = self.legacy_completed()
        news_reports.register_legacy_source_proof(self.store, row["id"], legacy)
        run = self.store.get("runs", "proof_run")
        for altered in ({**run, "read_evidence_ids": []}, {**run, "spoiler": True}, {**run, "status": "failed"}):
            self.store.put("runs", altered)
            self.store.save_settings({"spoiler_free": True})
            self.assertEqual(news_reports.migrate_report_boundaries(self.store)["preserved"], 0)
        self.store.put("runs", run)
        self.store.put("reports", {**row, "status": "stale"})
        self.assertEqual(news_reports.migrate_report_boundaries(self.store)["preserved"], 0)
        self.assertEqual(self.store.get("reports", row["id"])["boundary_migration"]["reason"], "previously_stale")


class PublicResearchTests(unittest.IsolatedAsyncioTestCase):
    def test_article_images_are_real_publisher_metadata_and_reject_private_or_active_urls(self):
        html = b'''<html><title>Training</title><meta property="og:image" content="https://cdn.example.com/lead.jpg"><article><figure><img src="https://cdn.example.com/team.jpg"><figcaption>Training session</figcaption></figure><p>Players return to training today and will prepare for the next game.</p></article></html>'''
        article = ingestion.extract_html(html, "https://example.com/news/training", "Club")[0]
        self.assertEqual(len(article["images"]), 2)
        self.assertEqual(article["images"][1]["caption"], "Training session")
        self.assertEqual(article["images"][0]["source_url"], article["url"])
        for url in ("http://cdn.example.com/photo.jpg", "https://127.0.0.1/a.jpg", "https://host.local/a.jpg", "data:image/png;base64,abc", "https://cdn.example.com/script.svg", "https://me:secret@cdn.example.com/a.jpg"):
            self.assertIsNone(ingestion.safe_image_url(url, article["url"]), url)

    async def test_public_search_accepts_allowed_publishers_and_explains_excluded_results(self):
        from tests.test_agent import FakeStore
        entries = [{"title": "Arsenal training", "url": "http://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fwww.arsenal.com%2Fnews%2Ftraining", "published_at": ingestion.now()},
                   {"title": "Untrusted", "url": "https://unknown.example.org/news/item"},
                   {"title": "Private", "url": "http://127.0.0.1/private"}]
        with patch("server.research._source_index", new=AsyncMock(return_value=(entries, [], {"state": "fresh", "checked_at": ingestion.now()}))):
            results, errors, coverage = await research._web_news_index(FakeStore(), "arsenal", "训练")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://www.arsenal.com/news/training")
        self.assertEqual(coverage["excluded_untrusted_count"], 2)
        self.assertEqual(errors, [])

    async def test_collect_moves_past_already_read_and_failed_candidates(self):
        from tests.test_agent import FakeStore
        candidates = [{"id": "old", "source_name": "Club", "last_read": "2026-09-01"}, {"id": "broken", "source_name": "Club"}, {"id": "new", "source_name": "Media"}]
        async def read(store, team, identifier, *args):
            if identifier == "broken":
                raise ingestion.IngestionError("source unavailable")
            return {"evidence": {"id": identifier}, "event": {"id": "event_" + identifier}, "duplicate": identifier == "old"}
        with patch("server.research.discover_news", new=AsyncMock(return_value={"candidates": candidates, "errors": [], "coverage": "partial"})), patch("server.research.read_candidate", side_effect=read) as reader:
            result = await research.collect_team(FakeStore(), "arsenal", limit=1)
        self.assertEqual(result["evidence_ids"], ["new"])
        self.assertEqual([call.args[2] for call in reader.await_args_list], ["broken", "new"])
        self.assertTrue(result["errors"])
