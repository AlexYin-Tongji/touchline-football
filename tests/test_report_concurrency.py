"""Independent editorial workers share a bounded, cancellable per-store queue."""
import asyncio
import unittest

from server import news_reports
from tests.test_agent import FakeStore
from tests.test_news_reports import event, successful_runner


class ParallelReportTests(unittest.IsolatedAsyncioTestCase):
    def store(self, count=3):
        store = FakeStore()
        for number in range(count):
            event(store, str(number))
        news_reports.enqueue_reports(store)
        return store

    async def test_two_reports_overlap_but_each_report_runs_once(self):
        store = self.store(5)
        active, maximum, seen = 0, 0, []
        async def runner(store, rid, cancel):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            seen.append(store.get("runs", rid)["report_id"])
            await asyncio.sleep(.025)
            await successful_runner(store, rid, cancel)
            active -= 1
        result = await news_reports.process_report_queue(store, runner=runner)
        self.assertEqual(maximum, 2)
        self.assertEqual(len(set(seen)), 5)
        self.assertEqual(result["completed"], 5)
        self.assertEqual(result["budget"]["concurrency"], 2)

    async def test_older_report_finishing_last_cannot_hide_current_completed_version(self):
        store = self.store(1)
        old_id = store.list("reports")[0]["id"]
        newer_finished = asyncio.Event()
        new_ids = []

        async def runner(store, rid, cancel):
            if store.get("runs", rid)["report_id"] == old_id:
                source = store.get("evidence", "ev_injury")
                source["text"] += " The medical team will assess the player again tomorrow."
                store.put("evidence", source)
                new_ids.append(news_reports.enqueue_reports(store)[0]["id"])
                await newer_finished.wait()
                await successful_runner(store, rid, cancel)
            else:
                await successful_runner(store, rid, cancel)
                newer_finished.set()

        await asyncio.wait_for(news_reports.process_report_queue(store, runner=runner), 1)
        self.assertEqual(store.get("reports", old_id)["status"], "stale")
        self.assertEqual(store.get("reports", new_ids[0])["status"], "completed")
        visible = news_reports.list_reports(store)
        self.assertEqual([(row["id"], row["status"]) for row in visible], [(new_ids[0], "completed")])
        self.assertTrue(visible[0]["report"]["sections"])

    async def test_independent_store_can_work_while_first_store_is_busy(self):
        first, second = self.store(1), self.store(1)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(store, rid, cancel):
            entered.set()
            await release.wait()
            await successful_runner(store, rid, cancel)
        task = asyncio.create_task(news_reports.process_report_queue(first, runner=slow))
        try:
            await entered.wait()
            other = await news_reports.process_report_queue(second, runner=successful_runner)
            self.assertFalse(other["busy"])
            self.assertEqual(other["completed"], 1)
        finally:
            release.set()
            await task

    async def test_shutdown_awaits_both_workers_and_leaves_no_generating_rows(self):
        store = self.store(4)
        entered, started = asyncio.Event(), []
        async def runner(store, rid, cancel):
            started.append(rid)
            if len(started) == 2:
                entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                store.put("runs", {**store.get("runs", rid), "status": "cancelled"})
                # The queue must still stop when a runner records cancellation.
        task = asyncio.create_task(news_reports.process_report_queue(store, runner=runner))
        try:
            await asyncio.wait_for(entered.wait(), .5)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(started), 2)
        self.assertFalse(any(row["status"] == "generating" for row in store.list("reports")))
        self.assertEqual(sum(row["status"] == "failed" for row in store.list("reports")), 2)
        self.assertFalse(news_reports._worker_lock(store).locked())

    async def test_one_failure_does_not_discard_other_reports(self):
        store = self.store(3)
        failed = False
        async def runner(store, rid, cancel):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("private provider response")
            await asyncio.sleep(.01)
            await successful_runner(store, rid, cancel)
        result = await news_reports.process_report_queue(store, runner=runner)
        self.assertEqual((result["completed"], result["failed"]), (2, 1))
        self.assertNotIn("private provider", str(store.list("reports")))

    async def test_max_jobs_limits_total_claims_across_workers(self):
        store = self.store(5)
        result = await news_reports.process_report_queue(store, runner=successful_runner, max_jobs=3)
        self.assertEqual(result["processed"], 3)
        self.assertEqual(sum(row["status"] == "queued" for row in store.list("reports")), 2)
