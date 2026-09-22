"""Bounded collection, shared request cancellation and article identity."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server import ingestion, research
from server.seed import seed
from server.storage import Store
from tests.test_research import SOURCE, ARTICLE


class CollectionConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "research.db")
        seed(self.store)
        self.store.put("sources", SOURCE)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def candidate(self, number=0):
        row = {"id": f"candidate_{number}", "mode": "live", "team_id": "arsenal",
               "url": f"https://example.com/news/article-{number}", "title": "Arsenal training",
               "source_id": SOURCE["id"], "source_name": SOURCE["name"]}
        self.store.put("candidates", row)
        return row

    async def test_collection_overlaps_reads_and_preserves_rank_order(self):
        candidates = [self.candidate(number) for number in range(3)]
        active = maximum = 0
        async def read(store, team, identifier, *args):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep({"candidate_0": .12, "candidate_1": .06, "candidate_2": .03}[identifier])
            active -= 1
            return {"evidence": {"id": identifier}, "duplicate": False}
        with patch.object(research, "discover_news", new=AsyncMock(return_value={"candidates": candidates, "errors": []})), patch.object(research, "read_candidate", side_effect=read):
            started = time.monotonic()
            result = await research.collect_team(self.store, "arsenal", limit=3)
            elapsed = time.monotonic() - started
        print(f"mock_collection_seconds={elapsed:.3f}; peak_reads={maximum}")
        self.assertEqual(result["evidence_ids"], [row["id"] for row in candidates])
        self.assertGreater(maximum, 1)
        self.assertLessEqual(maximum, 3)

    async def test_simultaneous_same_article_shares_fetch_and_atomic_ingest(self):
        candidate = self.candidate()
        async def fetch(*args):
            await asyncio.sleep(.04)
            return ARTICLE, candidate["url"], "text/html"
        with patch.object(research, "_fetch", side_effect=fetch) as request:
            results = await asyncio.gather(*(research.read_candidate(self.store, "arsenal", candidate["id"]) for _ in range(3)))
        self.assertEqual(request.await_count, 1)
        self.assertEqual(len({row["evidence"]["id"] for row in results}), 1)
        self.assertEqual(len(self.store.list("evidence", "live")), 1)

    async def test_failed_article_cools_down_but_explicit_refresh_retries(self):
        candidate = self.candidate()
        with patch.object(research, "_fetch", new=AsyncMock(side_effect=ingestion.IngestionError("HTTP 503"))) as request:
            for _ in range(2):
                with self.assertRaises(ingestion.IngestionError):
                    await research.read_candidate(self.store, "arsenal", candidate["id"])
            self.assertEqual(request.await_count, 1)
            with self.assertRaises(ingestion.IngestionError):
                await research.read_candidate(self.store, "arsenal", candidate["id"], force=True)
            self.assertEqual(request.await_count, 2)

    async def test_source_index_deduplicates_requests_and_cools_down_parse_failures(self):
        source = {**SOURCE, "kind": "rss"}
        feed = b'<rss><channel><item><title>Arsenal training</title><link>https://example.com/news/main</link></item></channel></rss>'
        async def fetch(*args):
            await asyncio.sleep(.03)
            return feed, source["url"], "application/rss+xml"
        with patch.object(research, "_fetch", side_effect=fetch) as request:
            rows = await asyncio.gather(*(research._source_index(self.store, "arsenal", source) for _ in range(2)))
        self.assertEqual(request.await_count, 1)
        self.assertEqual(rows[0][0], rows[1][0])
        with patch.object(research, "_fetch", new=AsyncMock(return_value=(b"broken", source["url"], "text/html"))) as request:
            await research._source_index(self.store, "arsenal", source, force=True)
            stale = await research._source_index(self.store, "arsenal", source)
            self.assertEqual(request.await_count, 1)
            self.assertEqual(stale[2]["state"], "stale")
            await research._source_index(self.store, "arsenal", source, force=True)
            self.assertEqual(request.await_count, 2)

    async def test_cancelling_one_shared_reader_preserves_other_reader(self):
        candidate = self.candidate()
        started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def fetch(*args):
            started.set()
            try:
                await release.wait()
                return ARTICLE, candidate["url"], "text/html"
            except asyncio.CancelledError:
                cancelled.set()
                raise
        with patch.object(research, "_fetch", side_effect=fetch) as request:
            first = asyncio.create_task(research.read_candidate(self.store, "arsenal", candidate["id"]))
            second = asyncio.create_task(research.read_candidate(self.store, "arsenal", candidate["id"]))
            await started.wait()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertFalse(cancelled.is_set())
            self.assertFalse(self.store.list("evidence", "live"))
            release.set()
            result = await second
        self.assertEqual(request.await_count, 1)
        self.assertTrue(result["evidence"])
        self.assertFalse(self.store._research_requests.pending)

    async def test_cancelling_last_shared_readers_cancels_fetch_without_late_writes(self):
        candidate = self.candidate()
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def fetch(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        with patch.object(research, "_fetch", side_effect=fetch):
            tasks = [asyncio.create_task(research.read_candidate(self.store, "arsenal", candidate["id"])) for _ in range(2)]
            await started.wait()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.store._research_requests.pending)
        self.assertFalse(self.store.list("evidence", "live"))
        self.assertNotIn("last_read", self.store.get("candidates", candidate["id"]))

    async def test_requests_bound_global_and_per_publisher_concurrency(self):
        from server.research_transport import shared_request
        release, capacity_reached = asyncio.Event(), asyncio.Event()
        active, peak, hosts, host_peaks = 0, 0, {}, {}
        async def fetch(host):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            hosts[host] = hosts.get(host, 0) + 1
            host_peaks[host] = max(host_peaks.get(host, 0), hosts[host])
            if active == 4:
                capacity_reached.set()
            try:
                await release.wait()
                return "ok"
            finally:
                active -= 1
                hosts[host] -= 1
        tasks = [asyncio.create_task(shared_request(self.store, (host, number), host, lambda h=host: fetch(h)))
                 for host in ("first.example", "second.example", "third.example") for number in range(3)]
        try:
            await asyncio.wait_for(capacity_reached.wait(), .5)
            self.assertEqual(peak, 4)
            self.assertTrue(all(value <= 2 for value in host_peaks.values()))
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(active, 0)
        self.assertFalse(self.store._research_requests.pending)

    async def test_source_disabled_during_fetch_is_not_imported(self):
        candidate = self.candidate()
        started, release = asyncio.Event(), asyncio.Event()
        async def fetch(*args):
            started.set()
            await release.wait()
            return ARTICLE, candidate["url"], "text/html"
        with patch.object(research, "_fetch", side_effect=fetch):
            task = asyncio.create_task(research.read_candidate(self.store, "arsenal", candidate["id"]))
            await started.wait()
            self.store.put("sources", {**SOURCE, "enabled": False})
            release.set()
            with self.assertRaisesRegex(ingestion.IngestionError, "停用"):
                await task
        self.assertFalse(self.store.list("evidence", "live"))

    async def test_reread_cannot_overwrite_privacy_change(self):
        item = ingestion.ingest(self.store, team_id="arsenal", title="Arsenal training", text="Arsenal returned to training ahead of the next match.",
                                url="https://example.com/news/main", source_id=SOURCE["id"], source_name=SOURCE["name"], origin="web")["evidence"]
        self.store._bodies.clear()
        started, release = asyncio.Event(), asyncio.Event()
        async def fetch(*args):
            started.set()
            await release.wait()
            return ARTICLE, item["url"], "text/html"
        with patch.object(research, "_fetch", side_effect=fetch):
            task = asyncio.create_task(research.read_evidence_source(self.store, item["id"]))
            await started.wait()
            self.store.put("evidence", {**self.store.get("evidence", item["id"]), "private": True})
            release.set()
            with self.assertRaises(ingestion.IngestionError):
                await task
        current = self.store.get("evidence", item["id"])
        self.assertTrue(current["private"])
        self.assertEqual(current["text"], "")

    async def test_partial_success_survives_unexpected_parser_failure(self):
        candidates = [self.candidate(number) for number in range(2)]
        async def read(store, team, identifier, *args):
            if identifier == "candidate_1":
                raise RuntimeError("untrusted secret url")
            return {"evidence": {"id": identifier}, "duplicate": False}
        with patch.object(research, "discover_news", new=AsyncMock(return_value={"candidates": candidates, "errors": [], "coverage": "available"})), patch.object(research, "read_candidate", side_effect=read):
            result = await research.collect_team(self.store, "arsenal", limit=2)
        self.assertEqual(result["evidence_ids"], ["candidate_0"])
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(len(result["errors"]), 1)
        self.assertNotIn("untrusted", str(result))

    async def test_fast_articles_persist_before_slow_article_and_cancel_stops_rest(self):
        candidates = [self.candidate(number) for number in range(3)]
        fast_finished, slow_started = asyncio.Event(), asyncio.Event()
        async def fetch(url, source):
            if url.endswith("article-0"):
                slow_started.set()
                await asyncio.Event().wait()
            fast_finished.set()
            return ARTICLE, url, "text/html"
        with patch.object(research, "discover_news", new=AsyncMock(return_value={"candidates": candidates, "errors": []})), patch.object(research, "_fetch", side_effect=fetch):
            task = asyncio.create_task(research.collect_team(self.store, "arsenal", limit=3))
            await asyncio.wait_for(asyncio.gather(fast_finished.wait(), slow_started.wait()), 1)
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertTrue(self.store.list("evidence", "live"))
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            count = len(self.store.list("evidence", "live"))
            await asyncio.sleep(.02)
        self.assertEqual(len(self.store.list("evidence", "live")), count)
        self.assertFalse(self.store._research_requests.pending)

    async def test_explicit_question_starts_search_without_waiting_for_publishers(self):
        search_started = asyncio.Event()
        async def publisher(*args, **kwargs):
            await asyncio.wait_for(search_started.wait(), .5)
            return [], [], {"state": "fresh", "checked_at": ingestion.now()}
        async def search(*args, **kwargs):
            search_started.set()
            return [], [], {"id": "bing_news", "name": "Search", "state": "fresh", "checked_at": ingestion.now()}
        with patch.object(research, "source_catalog", return_value=[SOURCE]), patch.object(research, "_source_index", side_effect=publisher), patch.object(research, "_web_news_index", side_effect=search), patch.object(research, "_espn_news_index", new=AsyncMock(return_value=([], [], {"id": "espn_news", "name": "ESPN", "state": "fresh", "checked_at": ingestion.now()}))):
            result = await research.discover_news(self.store, "arsenal", "training")
        self.assertEqual(result["errors"], [])

    async def test_manual_refresh_bypasses_recent_failure_and_retains_partial_details(self):
        from server.personal_app import PersonalRefreshManager
        from unittest.mock import Mock
        manager = PersonalRefreshManager(self.store, Mock())
        result = {"errors": ["Publisher: HTTP 503"], "evidence_ids": ["ev_success"], "coverage": "partial"}
        with patch.object(research, "collect_team", new=AsyncMock(return_value=result)) as collect, patch("server.news_reports.enqueue_reports"):
            for _ in range(2):
                self.assertTrue(manager.start("arsenal", force=True)["started"])
                await asyncio.gather(*manager.tasks.values())
        self.assertEqual(collect.await_count, 2)
        self.assertTrue(all(call.kwargs["force"] for call in collect.await_args_list))
        status = self.store.get_meta("personal_refresh_arsenal")
        self.assertEqual(status["source_errors"], result["errors"])
        self.assertEqual(status["collected_count"], 1)


class StructuredArticleIdentityTests(unittest.TestCase):
    def test_multiple_articles_keep_title_body_and_date_from_main_page(self):
        nodes = [
            {"@type": "NewsArticle", "url": "https://example.com/news/main", "headline": "Main update",
             "datePublished": "2026-09-21T10:00:00Z", "articleBody": "The captain resumed training. " * 20},
            {"@type": "NewsArticle", "url": "https://example.com/news/related", "headline": "Related contract",
             "datePublished": "2026-09-20T10:00:00Z", "articleBody": "The youth goalkeeper signed a contract. " * 25},
        ]
        html = '<html><title>Main update</title><script type="application/ld+json">' + json.dumps({"@graph": nodes}) + '</script></html>'
        row = ingestion.extract_html(html.encode(), "https://example.com/news/main", "Publisher")[0]
        self.assertEqual(row["title"], "Main update")
        self.assertIn("captain resumed", row["text"])
        self.assertNotIn("youth goalkeeper", row["text"])
        self.assertEqual(row["published_at"], "2026-09-21T10:00:00+00:00")

    def test_ambiguous_unbound_articles_fall_back_to_page_instead_of_mixing(self):
        nodes = [{"@type": "NewsArticle", "headline": title, "articleBody": title * 40,
                  "datePublished": "2026-09-21T10:00:00Z"} for title in ("Other player", "Another story")]
        html = '<html><title>Training today</title><script type="application/ld+json">' + json.dumps(nodes) + '</script><article>The current squad is preparing for its next match and training today.</article></html>'
        row = ingestion.extract_html(html.encode(), "https://example.com/news/main", "Publisher")[0]
        self.assertEqual(row["title"], "Training today")
        self.assertIn("current squad", row["text"])
        self.assertIsNone(row["published_at"])
