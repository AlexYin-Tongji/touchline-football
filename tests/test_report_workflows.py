"""Owned, team-scoped report batches using controllable local executors."""
import asyncio
import copy
import threading
import unittest

from server import news_reports
from server.agent_tools import evidence_passages
from tests.test_agent import FakeStore
from tests.test_news_reports import manuscript


def workflow_store(arsenal=2, liverpool=2):
    store = FakeStore()
    store.put("teams", {"id": "liverpool", "name": "利物浦", "english_name": "Liverpool"})
    for team, count, eid in (("arsenal", arsenal, "ev_injury"), ("liverpool", liverpool, "ev_other_team")):
        for index in range(count):
            store.put("events", {"id": f"event_{team}_{index}", "team_id": team, "mode": "live",
                                 "title": f"Medical update {index}", "summary": "", "evidence_ids": [eid]})
    news_reports.enqueue_reports(store)
    return store


async def complete(store, run_id, cancel):
    run = store.get("runs", run_id)
    eid = run["input_evidence_ids"][0]
    passage = evidence_passages(store.get("evidence", eid)["text"])[0]
    value = manuscript()
    for fact in value["facts"]:
        fact.update(evidence_ids=[eid], quotes=[{"evidence_id": eid, "passage_id": passage["id"]}])
    store.put("runs", {**run, "status": "completed", "structured_answer": value,
                       "evidence_ids": [eid], "read_evidence_ids": [eid], "verification": {"status": "supported"}})


class ReportWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_team_scope_owner_and_progress_do_not_touch_other_team(self):
        store = workflow_store()
        progress = []
        result = await news_reports.process_report_queue(store, team_id="arsenal", owner_id="workflow_a",
            runner=complete, on_progress=lambda value: progress.append(copy.deepcopy(value)))
        self.assertEqual((result["processed"], result["completed"]), (2, 2))
        self.assertEqual({row["team_id"] for row in store.list("runs")}, {"arsenal"})
        self.assertEqual({row["workflow_id"] for row in store.list("runs")}, {"workflow_a"})
        self.assertTrue(all(row["status"] == "queued" for row in store.list("reports", team_id="liverpool")))
        self.assertEqual([row["stage"] for row in progress].count("claim"), 2)
        self.assertEqual(progress[-1]["completed"], 2)
        self.assertNotIn("question", str(progress))

    async def test_different_teams_can_execute_together_but_share_two_slots(self):
        store = workflow_store(1, 1)
        entered, release = asyncio.Event(), asyncio.Event()
        active, peak, teams = 0, 0, set()
        async def runner(store, run_id, cancel):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            teams.add(store.get("runs", run_id)["team_id"])
            if len(teams) == 2:
                entered.set()
            try:
                await release.wait()
                await complete(store, run_id, cancel)
            finally:
                active -= 1
        tasks = [asyncio.create_task(news_reports.process_report_queue(store, team_id=team,
            owner_id=team, runner=runner)) for team in ("arsenal", "liverpool")]
        try:
            await asyncio.wait_for(entered.wait(), 1)
        finally:
            release.set()
            results = await asyncio.gather(*tasks)
        self.assertEqual(peak, 2)
        self.assertTrue(all(not row["busy"] and row["completed"] == 1 for row in results))

    async def test_global_and_team_batches_never_double_claim_or_exceed_store_capacity(self):
        store = workflow_store(4, 3)
        first_entered, release = asyncio.Event(), asyncio.Event()
        active, peak, seen = 0, 0, []
        async def runner(store, run_id, cancel):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            seen.append(store.get("runs", run_id)["report_id"])
            first_entered.set()
            try:
                await release.wait()
                await asyncio.sleep(0)
                await complete(store, run_id, cancel)
            finally:
                active -= 1
        first = asyncio.create_task(news_reports.process_report_queue(store, team_id="arsenal", owner_id="team-a", runner=runner))
        await first_entered.wait()
        duplicate = await news_reports.process_report_queue(store, team_id="arsenal", owner_id="duplicate", runner=runner)
        self.assertTrue(duplicate["busy"])
        others = [asyncio.create_task(news_reports.process_report_queue(store, owner_id="all", runner=runner)),
                  asyncio.create_task(news_reports.process_report_queue(store, team_id="liverpool", owner_id="team-b", runner=runner))]
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, *others)
        self.assertEqual(peak, 4)
        self.assertEqual(len(seen), 7)
        self.assertEqual(len(set(seen)), 7)
        self.assertEqual(sum(row["completed"] for row in results), 7)
        self.assertTrue(all(row["attempts"] == 1 for row in store.list("reports")))

    async def test_stopping_a_waiting_team_never_claims_or_executes_its_reports(self):
        store = workflow_store(2, 2)
        entered, release, cancel_b = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen = []
        async def runner(store, run_id, cancel):
            seen.append(store.get("runs", run_id)["team_id"])
            if len(seen) == 2:
                entered.set()
            await release.wait()
            await complete(store, run_id, cancel)
        first = asyncio.create_task(news_reports.process_report_queue(store, team_id="arsenal", owner_id="a", runner=runner))
        await entered.wait()
        waiting = asyncio.create_task(news_reports.process_report_queue(store, team_id="liverpool", owner_id="b",
            cancel_event=cancel_b, runner=runner))
        await asyncio.sleep(0)
        cancel_b.set()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(waiting, 1)
            self.assertEqual(seen, ["arsenal", "arsenal"])
            self.assertTrue(all(row["status"] == "queued" and row["attempts"] == 0
                                for row in store.list("reports", team_id="liverpool")))
        finally:
            release.set()
            await first
        resumed = await news_reports.process_report_queue(store, team_id="liverpool", owner_id="new-b", runner=complete)
        self.assertEqual(resumed["completed"], 2)

    async def test_event_stop_preserves_completed_work_and_does_not_cancel_another_owner(self):
        await self._stop_one_owner(use_event=True)

    async def test_caller_cancellation_preserves_completed_work_and_does_not_cancel_another_owner(self):
        await self._stop_one_owner(use_event=False)

    async def _stop_one_owner(self, use_event):
        store = workflow_store(3, 2)
        stop, blocking, other_started, other_release = (asyncio.Event() for _ in range(4))
        a_started = 0
        stopped = []
        async def runner(store, run_id, cancel):
            nonlocal a_started
            run = store.get("runs", run_id)
            if run["team_id"] == "arsenal":
                a_started += 1
                if a_started == 1:
                    return await complete(store, run_id, cancel)
                if a_started == 3:
                    blocking.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.append(run["workflow_id"])
            else:
                other_started.set()
                await other_release.wait()
                await complete(store, run_id, cancel)
        first = asyncio.create_task(news_reports.process_report_queue(store, team_id="arsenal", owner_id="a",
            cancel_event=stop, runner=runner))
        await asyncio.wait_for(blocking.wait(), 1)
        other = asyncio.create_task(news_reports.process_report_queue(store, team_id="liverpool", owner_id="b", runner=runner))
        if use_event:
            stop.set()
        else:
            first.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await first
            await asyncio.wait_for(other_started.wait(), 1)
            self.assertFalse(other.done())
            self.assertEqual(stopped, ["a", "a"])
            self.assertEqual(sum(row["status"] == "completed" for row in store.list("reports", team_id="arsenal")), 1)
            self.assertFalse(any(row["status"] == "generating" for row in store.list("reports", team_id="arsenal")))
        finally:
            other_release.set()
            result = await other
        self.assertEqual(result["completed"], 2)

    async def test_observer_failure_and_mutation_cannot_change_execution(self):
        store = workflow_store(2, 0)
        def broken(value):
            value["processed"] = 1000
            value["active_run_ids"].clear()
            raise ValueError("private callback failure")
        result = await news_reports.process_report_queue(store, team_id="arsenal", owner_id="a", runner=complete, on_progress=broken)
        self.assertEqual((result["processed"], result["completed"]), (2, 2))
        self.assertNotIn("private callback", str(store.list("reports")))

    async def test_already_stopped_batch_does_not_create_runs(self):
        store = workflow_store(1, 0)
        stop = asyncio.Event()
        stop.set()
        with self.assertRaises(asyncio.CancelledError):
            await news_reports.process_report_queue(store, team_id="arsenal", owner_id="a", cancel_event=stop, runner=complete)
        self.assertFalse(store.list("runs"))

    async def test_stop_adopts_a_verified_run_that_finished_just_before_cancellation(self):
        store = workflow_store(2, 0)
        stop = asyncio.Event()
        async def runner(store, run_id, cancel):
            await complete(store, run_id, cancel)
            stop.set()
        with self.assertRaises(asyncio.CancelledError):
            await news_reports.process_report_queue(store, team_id="arsenal", owner_id="a", cancel_event=stop, runner=runner)
        self.assertEqual(len(store.list("runs")), 1)
        self.assertEqual(sorted(row["status"] for row in store.list("reports")), ["completed", "queued"])

    async def test_repeated_cancellation_waits_for_runner_cleanup_before_unlocking(self):
        store = workflow_store(1, 0)
        entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))
        async def runner(store, run_id, cancel):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()
        batch = asyncio.create_task(news_reports.process_report_queue(store, team_id="arsenal", owner_id="a", runner=runner))
        await entered.wait()
        batch.cancel()
        await cleaning.wait()
        batch.cancel()
        await asyncio.sleep(0)
        self.assertFalse(batch.done())
        self.assertTrue(news_reports._worker_lock(store, "arsenal").locked())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await batch
        self.assertTrue(cleaned.is_set())
        self.assertFalse(news_reports._worker_lock(store, "arsenal").locked())
        self.assertEqual(store.list("reports")[0]["status"], "failed")

    async def test_progress_callback_cancel_error_is_isolated_from_batch_cancellation(self):
        store = workflow_store(1, 0)
        def observer(value):
            raise asyncio.CancelledError()
        result = await news_reports.process_report_queue(store, team_id="arsenal", runner=complete, on_progress=observer)
        self.assertEqual(result["completed"], 1)

    async def test_cancellation_on_final_unlock_is_not_swallowed_as_success(self):
        store = workflow_store(1, 0)
        underlying = threading.Lock()
        batch = None
        class CancelOnRelease:
            def acquire(self, **kwargs):
                return underlying.acquire(**kwargs)
            def release(self):
                underlying.release()
                batch.cancel()
        store._report_worker_lock = CancelOnRelease()
        batch = asyncio.create_task(news_reports.process_report_queue(store, runner=complete))
        with self.assertRaises(asyncio.CancelledError):
            await batch
        self.assertFalse(underlying.locked())
        self.assertEqual(store.list("reports")[0]["status"], "completed")
