"""Personal API contracts using isolated stores and mocked network/model work."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from server import ingestion, model_config, news_reports, team_center
from server.main import create_app
from tests.test_model_profiles import profile


CHINESE = "球队已经确认本周恢复训练，医疗团队将继续评估球员状态。下一场比赛的正式名单仍需等待公布。"
ENGLISH = "The club confirmed a training update. The medical team will continue assessing the players before the next match."


class PersonalAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.directory.name) / "personal.sqlite3")
        self.store = self.app.state.store
        # ASGITransport deliberately does not start scheduled workers. Individual
        # tests exercise the actual managers with explicit mocked providers.
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")

    async def asyncTearDown(self):
        await self.app.state.personal.close()
        await self.app.state.team_maintenance.close()
        tasks = [entry[0] for entry in self.app.state.run_tasks.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.aclose()
        self.store.close()
        self.directory.cleanup()

    def event(self, suffix, *, team="arsenal", chinese=False, completed=False, stamp="2026-09-16T10:00:00Z", spoiler=False):
        evidence = self.store.put("evidence", {"id": "ev_personal_" + suffix, "mode": "live", "team_id": team,
            "origin": "web", "title": "球队训练安排已有更新" if chinese else "Training update",
            "text": CHINESE if chinese else ENGLISH, "source_name": "测试来源", "url": "https://example.com/news/" + suffix,
            "published_at": stamp, "fetched_at": stamp, "spoiler": spoiler,
            "images": [{"url": "https://example.com/images/" + suffix + ".jpg", "credit": "测试来源", "source_url": "https://example.com/news/" + suffix}]})
        event = self.store.put("events", {"id": "event_personal_" + suffix, "mode": "live", "team_id": team,
            "title": "球队训练安排已有更新" if chinese else "Training update", "summary": CHINESE if chinese else ENGLISH,
            "evidence_ids": [evidence["id"]], "updated_at": stamp, "kind": "team", "spoiler": spoiler})
        report = None
        if completed:
            report = news_reports.enqueue_reports(self.store, team_id=team, event_ids=[event["id"]])[0]
            report.update(status="completed", verification_status="supported", report={
                "title": "球队训练安排已有更新", "lead": CHINESE,
                "sections": [{"heading": "训练情况", "body": CHINESE, "kind": "development", "evidence_ids": [evidence["id"]]}],
                "analysis": "后续名单仍需观察。", "next_steps": ["等待正式名单"], "images": evidence["images"],
                "sources": [{"id": evidence["id"], "title": evidence["title"], "url": evidence["url"], "source_name": "测试来源"}]})
            self.store.put("reports", report)
        return evidence, event, report

    async def feed(self, **params):
        response = await self.client.get("/api/feed", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_feed_shows_completed_chinese_reports_and_real_chinese_sources_only(self):
        _, ready, _ = self.event("ready", completed=True)
        _, pending, _ = self.event("pending")
        _, chinese, _ = self.event("chinese", chinese=True)
        # An old translated event excerpt is not a current readable source.
        self.store.put("events", {**pending, "title": "过去整理过的标题", "summary": "相关报道已收录，可查看来源，或请助手整理中文要点。"})
        feed = await self.feed(team_id="arsenal")
        self.assertEqual({item["id"] for item in feed["items"]}, {ready["id"], chinese["id"]})
        self.assertTrue(all("相关报道已收录" not in item["summary"] for item in feed["items"]))
        self.assertTrue(all(item["sources"] for item in feed["items"]))
        self.assertEqual(next(item for item in feed["items"] if item["id"] == ready["id"])["status"], "ready")

    async def test_feed_pagination_and_default_followed_team_scope(self):
        expected = []
        for number in range(5):
            _, event, _ = self.event(f"page_{number}", completed=True, stamp=f"2026-09-16T10:0{number}:00Z")
            expected.append(event["id"])
        _, barcelona, _ = self.event("barca", team="barcelona", completed=True)
        seen, cursor = [], None
        for _ in range(4):
            feed = await self.feed(limit=2, **({"cursor": cursor} if cursor else {}))
            seen.extend(item["id"] for item in feed["items"])
            cursor = feed["next_cursor"]
            if not cursor:
                break
        self.assertEqual(seen, list(reversed(expected)))
        self.assertEqual(len(seen), len(set(seen)))
        self.assertNotIn(barcelona["id"], seen)
        self.assertEqual([item["id"] for item in (await self.feed(team_id="barcelona"))["items"]], [barcelona["id"]])

    async def test_topic_filter_precedes_pagination(self):
        with self.store.atomic():
            for index in range(21):
                self.event(f"new_{index}", completed=True, stamp="2026-09-16T12:00:00Z")
            _, injury, report = self.event("older_injury", completed=True, stamp="2026-09-15T10:00:00Z")
            report["report"]["title"] = "球队伤病情况更新"
            self.store.put("reports", report)
        result = await self.feed(team_id="arsenal", topic="injury", limit=2)
        self.assertEqual([row["id"] for row in result["items"]], [injury["id"]])
        self.assertIsNone(result["next_cursor"])

    async def test_completed_feed_remains_readable_after_external_body_cache_expires(self):
        source, event, report = self.event("expired_body", completed=True)
        self.store._bodies.clear()
        self.assertEqual(self.store.get("evidence", source["id"])["text"], "")
        feed = await self.feed(team_id="arsenal")
        self.assertEqual([item["id"] for item in feed["items"]], [event["id"]])
        self.assertEqual(feed["items"][0]["report_id"], report["id"])
        self.assertEqual(feed["items"][0]["summary"], CHINESE)

    async def test_pagination_does_not_restart_when_cursor_event_is_deleted(self):
        records = [self.event(f"deletion_{i}", completed=True, stamp=f"2026-09-16T10:0{i}:00Z")[1] for i in range(4)]
        first = await self.feed(team_id="arsenal", limit=2)
        # Source changes may remove the item between scrolling requests.
        self.store.delete("events", first["items"][-1]["id"])
        second = await self.feed(team_id="arsenal", limit=2, cursor=first["next_cursor"])
        self.assertFalse({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})
        self.assertEqual({item["id"] for item in second["items"]}, {records[0]["id"], records[1]["id"]})

    async def test_private_deleted_or_hidden_source_cannot_leak_through_feed_or_report(self):
        source, event, report = self.event("private_marker", chinese=True, completed=True)
        event["images"] = source["images"]
        self.store.put("events", event)
        self.assertEqual(len((await self.feed(team_id="arsenal"))["items"]), 1)
        for flag in ({"private": True}, {"deleted": True}, {"visibility": "hidden"}, {"visibility": "deleted"}):
            with self.subTest(flag=flag):
                self.store.put("evidence", {**source, **flag})
                feed = await self.feed(team_id="arsenal")
                self.assertEqual(feed["items"], [])
                detail = await self.client.get("/api/reports/" + report["id"])
                self.assertEqual(detail.status_code, 200)
                self.assertIsNone(detail.json()["report"])
                self.assertNotIn(source["url"], detail.text)
                self.assertNotIn(source["images"][0]["url"], detail.text)
                self.store.put("evidence", source)
        self.store.delete("evidence", source["id"])
        self.assertEqual((await self.feed(team_id="arsenal"))["items"], [])

    async def test_hidden_foreign_sources_do_not_contribute_images_to_public_item(self):
        public, event, _ = self.event("public", chinese=True)
        private, _, _ = self.event("hidden_picture", chinese=True)
        self.store.put("evidence", {**private, "private": True})
        self.store.put("events", {**event, "evidence_ids": [public["id"], private["id"]], "images": private["images"]})
        feed = await self.feed(team_id="arsenal")
        self.assertEqual(len(feed["items"]), 1)
        self.assertNotIn("hidden_picture", json.dumps(feed))

    async def test_foreign_team_or_mode_evidence_does_not_make_event_visible(self):
        source, event, _ = self.event("foreign", team="barcelona", chinese=True)
        self.store.put("events", {**event, "id": "event_wrong_scope", "team_id": "arsenal"})
        self.assertEqual((await self.feed(team_id="arsenal"))["items"], [])
        self.store.put("evidence", {**source, "team_id": "arsenal", "mode": "demo"})
        self.assertEqual((await self.feed(team_id="arsenal"))["items"], [])

    async def test_spoiler_preference_cannot_be_bypassed_by_old_event_excerpt(self):
        source, event, _ = self.event("spoiler", chinese=True, completed=True)
        self.store.put("evidence", {**source, "title": "阿森纳以3-1赢得比赛", "text": "比赛已经结束，最终比分为3-1，阿森纳赢得比赛。", "spoiler": True})
        self.store.put("events", {**event, "title": "阿森纳以3-1赢得比赛", "summary": "最终比分3-1，阿森纳获胜。", "spoiler": True})
        self.store.save_settings({"spoiler_free": True})
        feed = await self.feed(team_id="arsenal")
        self.assertNotIn("3-1", json.dumps(feed, ensure_ascii=False))
        self.assertEqual(feed["items"], [])

    async def test_partially_redacted_source_does_not_reintroduce_result_images(self):
        source, _, _ = self.event("partial", chinese=True)
        self.store.put("evidence", {**source, "text": CHINESE * 4 + "\n\n球队最终以3-1赢得比赛，这是本场比赛已经确认的最终结果。",
            "spoiler": True, "images": [{"url": "https://example.com/images/3-1-score.jpg", "source_url": source["url"], "credit": "测试来源"}]})
        self.store.save_settings({"spoiler_free": True})
        feed = await self.feed(team_id="arsenal")
        self.assertEqual(len(feed["items"]), 1)
        self.assertEqual(feed["items"][0]["images"], [])
        self.assertNotIn("3-1", json.dumps(feed, ensure_ascii=False))

    async def test_incomplete_report_is_hidden_without_crashing_feed(self):
        _, _, report = self.event("incomplete", completed=True)
        report["report"]["lead"] = ""
        self.store.put("reports", report)
        feed = await self.feed(team_id="arsenal")
        self.assertEqual(feed["items"], [])

    async def test_evidence_endpoint_is_link_only_for_external_news_but_keeps_manual_and_fixtures(self):
        for origin, fixture, expected_body in (("web", False, False), ("rss", False, False), ("manual", False, True), ("web", True, True)):
            identity = f"evidence_{origin}_{int(fixture)}"
            self.store.put("evidence", {"id": identity, "mode": "live", "team_id": "arsenal", "origin": origin, "fixture_record": fixture,
                "title": "训练资料", "text": CHINESE, "url": "https://example.com/" + identity, "spoiler": False})
            response = await self.client.get("/api/evidence/" + identity)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(bool(response.json()["text"]), expected_body)
            if not expected_body:
                self.assertTrue(response.json()["link_only"])
                self.assertEqual(response.json()["paragraphs"], [])
                self.assertNotIn(CHINESE, response.text)

    async def test_evidence_private_and_deleted_records_are_not_public_links(self):
        for suffix, flags in (("private", {"private": True}), ("deleted", {"deleted": True}), ("hidden", {"visibility": "hidden"})):
            source, _, _ = self.event(suffix)
            self.store.put("evidence", {**source, **flags})
            response = await self.client.get("/api/evidence/" + source["id"])
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(source["url"], response.text)

    async def test_real_run_api_pins_model_across_selection_change_and_rejects_cross_scope(self):
        for item in (profile(), profile(id="beta", base_url="https://beta.example/v1", api_key="beta-only", models=[{"id": "beta-model"}])):
            response = await self.client.post("/api/model-providers", json=item.model_dump())
            self.assertEqual(response.status_code, 200, response.text)
        captured = []
        async def runner(store, identity, cancel):
            run = store.get("runs", identity)
            config = model_config.resolve_run_settings(store, run)["llm"]
            captured.append((run["id"], config["base_url"], config["model"]))
            store.put("runs", {**run, "status": "completed", "answer": "测试完成。"})
        base = {"mode": "live", "team_id": "arsenal", "question": "介绍球队", "allow_network": False}
        with patch("server.agent.run_agent", side_effect=runner):
            first = await self.client.post("/api/runs", json=base)
            self.assertEqual(first.status_code, 200, first.text)
            conversation = first.json()["conversation_id"]
            await asyncio.sleep(0)
            await self.client.post("/api/models/select", json={"provider_id": "beta", "model": "beta-model"})
            second = await self.client.post("/api/runs", json={**base, "conversation_id": conversation})
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual((second.json()["provider_id"], second.json()["model"]), ("alpha", "one"))
            await asyncio.sleep(0)
            changed = await self.client.post("/api/runs", json={**base, "conversation_id": conversation, "provider_id": "beta", "model": "beta-model"})
            self.assertEqual(changed.status_code, 409)
            foreign = await self.client.post("/api/runs", json={**base, "conversation_id": conversation, "team_id": "barcelona"})
            self.assertEqual(foreign.status_code, 400)
            fresh = await self.client.post("/api/runs", json=base)
            self.assertEqual(fresh.json()["provider_id"], "beta")
            await asyncio.sleep(0)
            await self.client.delete("/api/model-providers/alpha")
            removed = await self.client.post("/api/runs", json={**base, "conversation_id": conversation})
            self.assertEqual(removed.status_code, 400)
        self.assertEqual(captured[:2], [(first.json()["id"], "https://alpha.example/v1", "one"), (second.json()["id"], "https://alpha.example/v1", "one")])
        self.assertEqual(captured[2][1:], ("https://beta.example/v1", "beta-model"))

    async def test_center_get_and_personal_refresh_share_one_team_maintenance_job(self):
        release = asyncio.Event()
        async def roster(*args, **kwargs):
            await release.wait()
            return {"players": 0, "updated_at": ingestion.now()}
        async def collect(*args, **kwargs):
            await release.wait()
            return {"errors": [], "evidence_ids": []}
        with patch.object(team_center.league_data, "sync_league", new=AsyncMock(return_value={"sync_status": "completed"})), \
             patch.object(team_center, "sync_roster", side_effect=roster) as roster_fetch, \
             patch("server.research.collect_team", side_effect=collect) as news_fetch:
            first = await self.client.get("/api/teams/arsenal/center")
            self.assertEqual(first.status_code, 200)
            for _ in range(3):
                await self.client.get("/api/teams/arsenal/center")
                await self.client.post("/api/feed/refresh", json={"team_id": "arsenal"})
            for _ in range(4):
                await asyncio.sleep(0)
            self.assertEqual(roster_fetch.await_count, 1)
            self.assertEqual(news_fetch.await_count, 1)
            self.assertEqual(first.json()["team"]["id"], "arsenal")
            release.set()
            await asyncio.gather(*self.app.state.team_maintenance.tasks.values(), *self.app.state.personal.tasks.values())

    async def test_team_center_maintenance_concurrency_is_bounded_and_selected_scope_is_correct(self):
        release, count = asyncio.Event(), {"active": 0, "peak": 0}
        async def maintain(store, team_id, year=None, **kwargs):
            count["active"] += 1
            count["peak"] = max(count["peak"], count["active"])
            await release.wait()
            count["active"] -= 1
        with patch.object(team_center, "maintain_team", side_effect=maintain):
            for identity in ("arsenal", "barcelona", "ac-milan", "bournemouth"):
                response = await self.client.get(f"/api/teams/{identity}/center")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["team"]["id"], identity)
                self.assertTrue(all(player["team_id"] == identity for player in response.json()["roster"]))
            await asyncio.sleep(0)
            self.assertEqual(count["peak"], 2)
            release.set()
            await asyncio.gather(*self.app.state.team_maintenance.tasks.values())
        self.assertEqual((await self.client.get("/api/teams/missing/center")).status_code, 404)

    async def test_scheduler_reaches_seventh_followed_team_when_first_six_are_fresh(self):
        teams = [team["id"] for team in self.store.list("teams")][:7]
        self.store.save_settings({"followed_teams": teams})
        for identity in teams[:6]:
            self.store.set_meta("personal_refresh_" + identity, {"last_attempt": ingestion.now(), "status": "idle"})
        with patch.object(self.app.state.team_maintenance, "start", return_value={"started": False}), \
             patch("server.research.collect_team", new=AsyncMock(return_value={"errors": [], "evidence_ids": []})) as collect:
            result = self.app.state.personal.start()
            self.assertTrue(result["started"])
            await asyncio.gather(*self.app.state.personal.tasks.values())
        self.assertEqual([call.args[1] for call in collect.await_args_list], [teams[6]])

    async def test_personal_shutdown_before_job_starts_clears_running_marker(self):
        self.app.state.personal.start("arsenal")
        await self.app.state.personal.close()
        self.assertNotEqual(self.store.get_meta("personal_refresh_arsenal", {}).get("status"), "running")


if __name__ == "__main__":
    unittest.main()
