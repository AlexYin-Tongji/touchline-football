import asyncio
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from server import ingestion
from server.main import create_app
from server.storage import Store


TEXT = "球队训练消息只确认球员完成部分合练，教练没有宣布正式比赛名单。目前仍然需要等待后续消息，不能将训练参与等同于出场许可。"


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "test.sqlite3"
        self.app = create_app(self.path)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.directory.cleanup()

    def import_item(self, **values):
        response = self.client.post("/api/import", json={"team_id": "arsenal", "title": "训练情况与后续确认", "text": TEXT, **values})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_demo_and_live_never_mix_and_unknown_date_remains_unknown(self):
        initial = self.client.get("/api/state?mode=live").json()
        self.assertEqual(initial["events"], [])
        self.assertEqual(initial["evidence"], [])
        self.assertTrue(self.client.get("/api/state?mode=demo").json()["events"])
        imported = self.import_item()
        self.assertIsNone(imported["evidence"]["published_at"])
        live = self.client.get("/api/state?mode=live").json()
        self.assertEqual(len(live["evidence"]), 1)
        self.assertEqual(live["evidence"][0]["origin"], "manual")
        self.assertTrue(all(item["mode"] == "demo" for item in self.client.get("/api/state?mode=demo").json()["evidence"]))
        self.assertEqual(self.client.get("/api/state?mode=oops").status_code, 422)

    def test_duplicate_normalization_and_event_stage_progression(self):
        first = self.import_item(stage="训练消息", url="https://example.com/article")
        event_id = first["event"]["id"]
        self.client.post(f"/api/events/{event_id}/watch", json={"watching": True})
        self.client.post(f"/api/events/{event_id}/read", json={"read": True})
        duplicate = self.import_item(text="  " + TEXT + "\n", source_name="另一个转载来源")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["evidence"]["id"], first["evidence"]["id"])
        next_item = self.import_item(title="名单确认暂不出场", text=TEXT + "俱乐部随后公布名单，该球员本轮没有进入比赛名单。", event_id=event_id, stage="名单已确认")
        self.assertFalse(next_item["duplicate"])
        self.assertEqual(next_item["event"]["stage"], "名单已确认")
        self.assertEqual(next_item["event"]["title"], "名单确认暂不出场")
        self.assertTrue(next_item["event"]["watching"])
        self.assertFalse(next_item["event"]["read"])
        self.assertEqual(len(next_item["event"]["timeline"]), 2)
        self.assertEqual(next_item["event"]["timeline"][0]["evidence_id"], first["evidence"]["id"])
        third = self.import_item(text=TEXT + "新增更完整训练资料，没有补充具体医学诊断。", event_id=event_id)
        self.assertEqual(third["event"]["stage"], "名单已确认")

    def test_url_update_joins_event_and_cross_team_append_is_rejected(self):
        first = self.import_item(url="https://example.com/article#top")
        second = self.import_item(url="https://example.com/article", text=TEXT + "第二次更新加入了记者发布的完整采访原文。")
        self.assertEqual(first["event"]["id"], second["event"]["id"])
        response = self.client.post("/api/import", json={"team_id": "barcelona", "title": "不能跨球队追加", "text": TEXT, "event_id": first["event"]["id"]})
        self.assertEqual(response.status_code, 400)

    def test_duplicate_can_upgrade_spoiler_without_new_event_or_timeline(self):
        original = self.import_item()
        upgraded = self.import_item(spoiler=True)
        self.assertTrue(upgraded["duplicate"])
        self.assertTrue(upgraded["evidence"]["spoiler"])
        self.assertTrue(upgraded["event"]["spoiler"])
        self.assertEqual(upgraded["event"]["timeline"], [])
        self.assertTrue(upgraded["event"]["spoiler_hidden"])
        self.assertEqual(len(self.app.state.store.get("events", upgraded["event"]["id"])["timeline"]), 1)
        self.assertEqual(upgraded["evidence"]["id"], original["evidence"]["id"])
        self.assertEqual(self.client.get("/api/state?mode=live").json()["evidence"], [])

    def test_credentials_are_encrypted_and_origin_changes_do_not_reuse_them(self):
        secret = "sk-unit-test-private-123456789"
        response = self.client.put("/api/settings", json={"explanation_depth": "deep", "llm": {"base_url": "http://localhost:8000/v1", "model": "local-model", "api_key": secret}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["llm"]["api_key_set"])
        self.assertNotIn(secret, response.text)
        same = self.client.put("/api/settings", json={"llm": {"base_url": "http://localhost:8000/another", "api_key": ""}}).json()
        self.assertTrue(same["llm"]["api_key_set"])
        self.assertNotIn(secret, self.client.get("/api/state").text)
        for file in Path(self.directory.name).glob("*.sqlite3*"):
            self.assertNotIn(secret.encode(), file.read_bytes())
        changed = self.client.put("/api/settings", json={"llm": {"base_url": "http://localhost:9000/v1", "api_key": ""}}).json()
        self.assertFalse(changed["llm"]["api_key_set"])
        self.assertEqual(changed["explanation_depth"], "deep")
        self.assertEqual(self.app.state.store.settings()["llm"]["api_key"], "")

    def test_test_connection_uses_unsaved_values_without_leaking_saved_key(self):
        self.client.put("/api/settings", json={"llm": {"base_url": "https://provider.example/v1", "model": "saved", "api_key": "private-test-key"}})
        probe = AsyncMock(return_value={"ok": True, "message": "连接可用", "tool_support": True})
        with patch("server.agent.test_connection", probe):
            result = self.client.post("/api/settings/test", json={"base_url": "http://localhost:8000/v1", "model": "unsaved", "api_key": ""})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(probe.call_args.args[0]["api_key"], "")
        self.assertEqual(probe.call_args.args[0]["model"], "unsaved")
        self.assertEqual(self.app.state.store.settings()["llm"]["model"], "saved")

    def test_settings_bounds_and_import_timestamp_validation(self):
        for payload in ({"llm": {"max_rounds": 17}}, {"refresh_minutes": 4}, {"spoiler_free": "false"}, {"followed_teams": []}, {"explanation_depth": "verbose"}):
            self.assertEqual(self.client.put("/api/settings", json=payload).status_code, 422)
        response = self.client.post("/api/import", json={"team_id": "arsenal", "title": "时间不能推测", "text": TEXT, "published_at": "2026-09-09T10:00:00"})
        self.assertEqual(response.status_code, 422)
        self.assertIsInstance(response.json()["detail"], str)
        self.assertEqual(self.client.put("/api/settings", json={"followed_teams": ["missing"]}).status_code, 404)
        self.client.put("/api/settings", json={"mode": "live"})
        restored = self.client.get("/api/state").json()
        self.assertEqual(restored["settings"]["mode"], "live")
        self.assertEqual(restored["evidence"], [])
        self.assertTrue(self.client.get("/api/state?mode=demo").json()["evidence"])

    def test_source_private_addresses_credentials_and_scheme_are_rejected(self):
        for url in ("http://127.0.0.1/news", "http://[::1]/news", "http://169.254.169.254/latest/meta-data", "http://10.0.0.1/rss", "http://localhost/news", "file:///etc/passwd", "https://user:pass@example.com/rss"):
            response = self.client.post("/api/sources", json={"team_id": "arsenal", "name": "受限来源", "url": url, "kind": "rss"})
            self.assertEqual(response.status_code, 422, url)
        self.assertEqual(self.client.post("/api/import", content="x" * (256 * 1024 + 1), headers={"Content-Type": "application/json"}).status_code, 413)
        self.assertEqual(self.client.post("/api/import", json={}, headers={"Origin": "https://attacker.example"}).status_code, 403)

    def test_source_refresh_error_is_truthful_and_success_deduplicates(self):
        source = self.client.post("/api/sources", json={"team_id": "arsenal", "name": "示例来源", "url": "https://example.com/article", "kind": "html"}).json()
        with patch("server.ingestion.fetch_document", side_effect=ingestion.IngestionError("来源请求超时，请稍后重试")):
            result = self.client.post(f"/api/sources/{source['id']}/refresh").json()
        self.assertEqual(result["added"], 0)
        self.assertTrue(result["errors"])
        stored = self.app.state.store.get("sources", source["id"])
        self.assertIsNone(stored["last_success"])
        self.assertTrue(stored["last_checked"])
        body = ("<html><title>Training update</title><article>" + TEXT + "</article></html>").encode()
        with patch("server.ingestion.fetch_document", return_value=(body, source["url"], "text/html")):
            first = self.client.post(f"/api/sources/{source['id']}/refresh").json()
            second = self.client.post(f"/api/sources/{source['id']}/refresh").json()
        self.assertEqual(first["added"], 1)
        self.assertEqual(second["unchanged"], 1)
        self.assertFalse(second["errors"])
        self.assertIsNotNone(self.app.state.store.get("sources", source["id"])["last_success"])
        self.client.delete(f"/api/sources/{source['id']}")
        self.assertEqual(len(self.app.state.store.list("evidence", "live")), 1)
        with patch("server.ingestion.fetch_document") as network:
            result = self.client.post("/api/refresh", json={"mode": "demo"}).json()
            network.assert_not_called()
            self.assertIn("虚构", result["message"])

    def test_spoiler_state_filters_events_evidence_matches_and_answers(self):
        item = self.import_item(title="赛后比分 2-1", text=TEXT + "这份手动资料补充了最终比分。", spoiler=True)
        match = self.client.post("/api/matches", json={"team_id": "arsenal", "opponent": "测试对手", "competition": "手动赛事", "kickoff": "2026-09-08T18:00:00Z", "venue": "home", "status": "finished", "score": "2-1", "note": "赢球之后的记录", "evidence_ids": [item["evidence"]["id"]]}).json()
        self.app.state.store.put("runs", {"id": "run_spoiler", "mode": "live", "team_id": "arsenal", "skill": "match_review", "match_id": match["id"], "status": "completed", "answer": "2-1获胜", "evidence_ids": [item["evidence"]["id"]], "spoiler": True})
        state = self.client.get("/api/state?mode=live").json()
        self.assertEqual(state["events"], [])
        self.assertEqual(state["evidence"], [])
        self.assertEqual(state["runs"], [])
        self.assertIsNone(state["matches"][0]["score"])
        self.assertEqual(state["matches"][0]["note"], "")
        self.assertTrue(state["matches"][0]["spoiler_hidden"])
        revealed = self.client.get("/api/runs/run_spoiler").json()
        self.assertEqual(revealed["evidence"][0]["id"], item["evidence"]["id"])
        self.client.put("/api/settings", json={"spoiler_free": False})
        self.assertEqual(self.client.get("/api/state?mode=live").json()["matches"][0]["score"], "2-1")

    def test_match_evidence_scope_and_review_status_are_validated(self):
        item = self.import_item()
        payload = {"team_id": "barcelona", "opponent": "测试对手", "competition": "手动赛事", "kickoff": "2026-09-12T18:00:00Z", "venue": "home", "evidence_ids": [item["evidence"]["id"]]}
        self.assertEqual(self.client.post("/api/matches", json=payload).status_code, 400)
        payload.update({"team_id": "arsenal"})
        match = self.client.post("/api/matches", json=payload).json()
        invalid = self.client.post("/api/runs", json={"mode": "live", "team_id": "arsenal", "question": "帮我复盘", "skill": "match_review", "match_id": match["id"]})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.client.patch(f"/api/matches/{match['id']}", json={"score": "2-1"}).status_code, 422)
        finished = self.client.patch(f"/api/matches/{match['id']}", json={"status": "finished", "score": "2-1"})
        self.assertEqual(finished.status_code, 200)

    def test_match_patch_preserves_hidden_evidence_but_removes_visible_selection(self):
        visible = self.import_item()
        hidden = self.import_item(text=TEXT + "赛后记录已结束，最后比分为 2-1。", spoiler=True)
        response = self.client.post("/api/matches", json={"team_id": "arsenal", "opponent": "测试对手", "competition": "手动赛事", "kickoff": "2026-09-08T18:00:00Z", "venue": "home", "status": "finished", "score": "2-1", "note": "需要保留", "evidence_ids": [visible["evidence"]["id"], hidden["evidence"]["id"]]})
        match = response.json()
        updated = self.client.patch(f"/api/matches/{match['id']}", json={"evidence_ids": []}).json()
        self.assertEqual(updated["evidence_ids"], [hidden["evidence"]["id"]])
        self.assertEqual(updated["score"], "2-1")
        self.assertEqual(updated["note"], "需要保留")
        self.client.put("/api/settings", json={"spoiler_free": False})
        cleared = self.client.patch(f"/api/matches/{match['id']}", json={"evidence_ids": [], "score": None, "note": ""}).json()
        self.assertEqual(cleared["evidence_ids"], [])
        self.assertIsNone(cleared["score"])

    def test_async_run_and_cancel_remain_persisted(self):
        async def slow_agent(store, run_id, cancel_event):
            run = store.get("runs", run_id)
            run["status"] = "running"
            store.put("runs", run)
            await asyncio.sleep(60)
        with patch("server.agent.run_agent", slow_agent):
            response = self.client.post("/api/runs", json={"mode": "demo", "team_id": "arsenal", "question": "下一场看什么", "skill": "ask"})
            self.assertEqual(response.status_code, 200)
            identifier = response.json()["id"]
            stopped = self.client.post(f"/api/runs/{identifier}/cancel")
            self.assertEqual(stopped.json()["status"], "cancelled")
        self.assertEqual(self.client.get(f"/api/runs/{identifier}").json()["status"], "cancelled")


class ParserTests(unittest.TestCase):
    def test_json_ld_date_published_and_no_modified_date_substitution(self):
        document = ('<html><title>Training</title><script type="application/ld+json">' + json.dumps({"@graph": [{"@type": "NewsArticle", "datePublished": "2026-09-08T18:28:00.000Z", "dateModified": "2026-09-09T00:00:00Z"}]}) + '</script><article>' + TEXT + '</article></html>').encode()
        parsed = ingestion.extract_html(document, "https://example.com/article", "Example")[0]
        self.assertEqual(parsed["published_at"], "2026-09-08T18:28:00+00:00")
        modified_only = document.replace(b'datePublished', b'notPublished')
        self.assertIsNone(ingestion.extract_html(modified_only, "https://example.com/article", "Example")[0]["published_at"])
        self.assertNotIn("datePublished", parsed["text"])

    def test_rss_and_atom_extract_original_time_and_plain_text(self):
        rss = ('<rss><channel><item><title>Training</title><link>https://example.com/news</link><pubDate>Tue, 08 Sep 2026 18:28:00 GMT</pubDate><description><![CDATA[<p>' + TEXT + '</p><script>alert(1)</script>]]></description></item></channel></rss>').encode()
        item = ingestion.extract_feed(rss, "https://example.com/feed", "Example")[0]
        self.assertEqual(item["published_at"], "2026-09-08T18:28:00+00:00")
        self.assertNotIn("alert", item["text"])
        atom = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Training</title><link href="/news"/><updated>2026-09-09T12:00:00Z</updated><content>' + TEXT + '</content></entry></feed>').encode()
        parsed = ingestion.extract_feed(atom, "https://example.com/feed", "Example")[0]
        self.assertIsNone(parsed["published_at"])
        self.assertEqual(parsed["url"], "https://example.com/news")
        with self.assertRaises(ingestion.IngestionError):
            ingestion.extract_feed(b'<!DOCTYPE rss [<!ENTITY x "unsafe">]><rss/>', "https://example.com", "Example")

    def test_dns_private_result_is_blocked_and_public_address_is_pinned(self):
        with patch("server.ingestion.socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(ingestion.IngestionError):
                ingestion.public_addresses("safe-looking.example", 443)
        with patch("server.ingestion.socket.create_connection") as connect:
            connection = ingestion._PinnedHTTP("safe-looking.example", 80, "93.184.216.34", False, 5)
            connection.connect()
            connect.assert_called_once_with(("93.184.216.34", 80), timeout=5)

    def test_redirect_to_private_network_is_rejected_before_connection(self):
        class Response:
            status = 302
            def getheader(self, name, default=None):
                return "http://169.254.169.254/latest" if name == "Location" else default
        class Connection:
            def request(self, *args, **kwargs): pass
            def getresponse(self): return Response()
            def close(self): pass
        with patch("server.ingestion.public_addresses", return_value=["93.184.216.34"]), patch("server.ingestion._PinnedHTTP", return_value=Connection()) as connector:
            with self.assertRaises(ingestion.IngestionError):
                ingestion.fetch_document("https://example.com/news")
            self.assertEqual(connector.call_count, 1)

    def test_loading_page_is_not_news_and_spoiler_detection_is_conservative(self):
        with self.assertRaises(ingestion.IngestionError):
            ingestion.extract_html(b"<html><main>Please enable JavaScript to continue. Checking your browser before accessing this website.</main></html>", "https://example.com", "Example")
        self.assertTrue(ingestion.potential_spoiler("Match report", "Arsenal 2-1 Opponent"))
        self.assertFalse(ingestion.potential_spoiler("Training update", "The club has not announced the squad yet."))


if __name__ == "__main__":
    unittest.main()
