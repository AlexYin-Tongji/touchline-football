"""Generic task-tree behavior, with no model, database, or business dependencies."""
import asyncio
import time
import unittest

from server.agent_runtime import AgentRuntime, RuntimeAccessError, RuntimeStateError


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.observed = []
        self.runtime = AgentRuntime(observer=self.observed.append, max_active_per_root=3)
        self.cancel_event = asyncio.Event()
        self.deadline = time.monotonic() + 10
        self.runtime.register_root("root", cancel_event=self.cancel_event, deadline=self.deadline)

    async def asyncTearDown(self):
        if hasattr(self, "runtime"):
            await self.runtime.shutdown()

    async def settled(self, parent, ids):
        cursor = 0
        for _ in range(30):
            value = await self.runtime.wait(parent, ids, after=cursor, timeout=1)
            if all(row["status"] in {"completed", "failed", "cancelled"} for row in value["agents"]):
                return value
            cursor = value["cursor"]
        self.fail("Tasks did not settle")

    async def test_children_overlap_and_one_failure_does_not_cancel_sibling(self):
        started, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def work(context):
            nonlocal count
            count += 1
            if count == 2:
                started.set()
            await release.wait()
            if context.payload == "fail":
                raise ValueError("private upstream body")
            return {"value": context.payload, "deadline": context.deadline}
        one = self.runtime.spawn("root", execute=work, payload="fail")
        two = self.runtime.spawn("root", execute=work, payload="ok")
        await asyncio.wait_for(started.wait(), 1)
        release.set()
        rows = (await self.settled("root", [one["id"], two["id"]]))["agents"]
        self.assertEqual([row["status"] for row in rows], ["failed", "completed"])
        self.assertEqual(rows[1]["result"], {"value": "ok", "deadline": self.deadline})
        self.assertNotIn("private upstream", str(rows))

    async def test_only_direct_parent_can_manage_child(self):
        entered, release = asyncio.Event(), asyncio.Event()
        child_id = None
        async def leaf(context):
            await release.wait()
        async def parent(context):
            nonlocal child_id
            child_id = self.runtime.spawn(context.id, execute=leaf, payload={})["id"]
            entered.set()
            await release.wait()
        direct = self.runtime.spawn("root", execute=parent, payload={})
        await entered.wait()
        self.runtime.register_root("other", cancel_event=asyncio.Event(), deadline=self.deadline)
        for caller, target in [("other", direct["id"]), ("root", child_id)]:
            with self.assertRaises(RuntimeError):
                self.runtime.inspect(caller, [target])
            with self.assertRaises(RuntimeError):
                self.runtime.send(caller, target, "secret")
            with self.assertRaises(RuntimeError):
                await self.runtime.cancel(caller, target)
        self.assertEqual(self.runtime.inspect(direct["id"], [child_id])["agents"][0]["id"], child_id)

    async def test_message_order_idempotency_and_followup_generation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        contexts = []
        async def work(context):
            contexts.append(context)
            if context.generation == 1:
                entered.set()
                await release.wait()
            return {"messages": [item["message"] for item in self.runtime.drain_inbox(context.id)]}
        child = self.runtime.spawn("root", execute=work, payload={"seed": "private"}, request_id="spawn")
        duplicate = self.runtime.spawn("root", execute=work, payload={"seed": "private"}, request_id="spawn")
        self.assertEqual(child["id"], duplicate["id"])
        with self.assertRaises(RuntimeError):
            self.runtime.spawn("root", execute=work, payload={"seed": "different"}, request_id="spawn")
        async def different_execution(context):
            return {"different": True}
        with self.assertRaises(RuntimeError):
            self.runtime.spawn("root", execute=different_execution, payload={"seed": "private"}, request_id="spawn")
        await entered.wait()
        self.runtime.send("root", child["id"], "first", request_id="message")
        self.runtime.send("root", child["id"], "first", request_id="message")
        with self.assertRaises(RuntimeError):
            self.runtime.send("root", child["id"], "changed", request_id="message")
        self.runtime.send("root", child["id"], "second")
        release.set()
        row = (await self.settled("root", [child["id"]]))["agents"][0]
        self.assertEqual(row["result"], {"messages": ["first", "second"]})
        followup = self.runtime.send("root", child["id"], "third")
        self.assertEqual(followup["generation"], 2)
        row = (await self.settled("root", [child["id"]]))["agents"][0]
        self.assertEqual(row["result"], {"messages": ["third"]})
        self.assertEqual(contexts[1].previous_result, {"messages": ["first", "second"]})
        self.assertEqual(contexts[1].deadline, self.deadline)

    async def test_message_arriving_after_last_drain_is_not_lost(self):
        drained, release = asyncio.Event(), asyncio.Event()
        async def work(context):
            messages = self.runtime.drain_inbox(context.id)
            if context.generation == 1:
                drained.set()
                await release.wait()
            return [item["message"] for item in messages]
        child = self.runtime.spawn("root", execute=work, payload={})
        await drained.wait()
        self.runtime.send("root", child["id"], "late")
        release.set()
        row = (await self.settled("root", [child["id"]]))["agents"][0]
        self.assertEqual(row["generation"], 2)
        self.assertEqual(row["result"], ["late"])

    async def test_wait_timeout_keeps_children_running_and_cursor_catches_completion(self):
        release = asyncio.Event()
        async def work(context):
            await release.wait()
            return "private result"
        child = self.runtime.spawn("root", execute=work, payload="private prompt")
        await asyncio.sleep(0)
        before = self.runtime.inspect("root")
        timed = await self.runtime.wait("root", after=before["cursor"], timeout=.01)
        self.assertTrue(timed["timed_out"])
        self.assertEqual(timed["agents"][0]["status"], "running")
        release.set()
        await self.settled("root", [child["id"]])
        caught = await self.runtime.wait("root", after=before["cursor"], timeout=.01)
        self.assertFalse(caught["timed_out"])
        self.assertTrue(any(e["status"] == "completed" for e in caught["events"]))
        self.assertNotIn("private", str(self.observed))
        self.assertNotIn("private", str(self.runtime.snapshot()))

    async def test_waiting_parent_does_not_prevent_descendants_running(self):
        async def leaf(context):
            return "leaf"
        async def branch(context):
            child = self.runtime.spawn(context.id, execute=leaf, payload={})
            result = await self.settled(context.id, [child["id"]])
            return result["agents"][0]["result"]
        node = self.runtime.spawn("root", execute=branch, payload={})
        row = (await self.settled("root", [node["id"]]))["agents"][0]
        self.assertEqual(row["result"], "leaf")

    async def test_cancel_subtree_and_normal_parent_exit_clean_descendants(self):
        entered = asyncio.Event()
        stopped = []
        async def leaf(context):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(context.id)
        async def parent(context):
            self.runtime.spawn(context.id, execute=leaf, payload={})
            await asyncio.Event().wait()
        node = self.runtime.spawn("root", execute=parent, payload={})
        await entered.wait()
        await self.runtime.cancel("root", node["id"])
        self.assertEqual(len(stopped), 1)
        self.assertEqual(self.runtime.inspect("root")["agents"][0]["status"], "cancelled")
        entered.clear()
        async def normal_parent(context):
            self.runtime.spawn(context.id, execute=leaf, payload={})
            await entered.wait()
            return "done"
        normal = self.runtime.spawn("root", execute=normal_parent, payload={})
        await self.settled("root", [normal["id"]])
        self.assertEqual(len(stopped), 2)

    async def test_capacity_is_reclaimed_without_lifetime_task_quota(self):
        release = asyncio.Event()
        async def hold(context):
            await release.wait()
        nodes = [self.runtime.spawn("root", execute=hold, payload={}) for _ in range(3)]
        with self.assertRaises(RuntimeError):
            self.runtime.spawn("root", execute=hold, payload={})
        release.set()
        await self.settled("root", [n["id"] for n in nodes])
        for _ in range(15):
            node = self.runtime.spawn("root", execute=hold, payload={})
            await self.settled("root", [node["id"]])

    async def test_expired_or_cancelled_root_cannot_spawn_and_finish_releases_contexts(self):
        calls = []
        async def work(context):
            calls.append(context.id)
            return "private result"
        self.runtime.register_root("expired", cancel_event=asyncio.Event(), deadline=time.monotonic() - 1)
        with self.assertRaises(RuntimeError):
            self.runtime.spawn("expired", execute=work, payload={})
        child = self.runtime.spawn("root", execute=work, payload="private prompt")
        await self.settled("root", [child["id"]])
        await self.runtime.finish_root("root", "completed")
        with self.assertRaises(RuntimeError):
            self.runtime.inspect("root")
        self.assertNotIn("private", str(self.runtime.snapshot()))
        self.assertFalse(any(item["root_id"] == "root" for item in self.runtime.snapshot()["active"]))
        self.cancel_event.set()

    async def test_root_event_cancels_children_and_depth_is_bounded(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def hold(context):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.runtime.spawn("root", execute=hold, payload={})
        await started.wait()
        self.cancel_event.set()
        await asyncio.wait_for(stopped.wait(), 1)
        with self.assertRaises(RuntimeError):
            self.runtime.spawn("root", execute=hold, payload={})

    async def test_depth_limit_fails_locally_without_stopping_parent(self):
        async def nested(context):
            if context.payload == 3:
                try:
                    self.runtime.spawn(context.id, execute=nested, payload=4)
                except RuntimeError:
                    return "depth rejected"
                return "unexpected extra level"
            child = self.runtime.spawn(context.id, execute=nested, payload=context.payload + 1)
            rows = await self.settled(context.id, [child["id"]])
            return rows["agents"][0]["result"]
        child = self.runtime.spawn("root", execute=nested, payload=1)
        row = (await self.settled("root", [child["id"]]))["agents"][0]
        self.assertEqual(row["result"], "depth rejected")

    async def test_root_finish_is_safe_when_shutdown_and_owner_finish_overlap(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def work(context):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        self.runtime.spawn("root", execute=work, payload={})
        await entered.wait()
        first = asyncio.create_task(self.runtime.finish_root("root", "completed"))
        await cleaning.wait()
        second = asyncio.create_task(self.runtime.finish_root("root", "interrupted"))
        await asyncio.sleep(0)
        release.set()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual(outcomes, [None, None])
        self.assertFalse(self.runtime.snapshot()["active"])

    async def test_cancelled_finish_waiter_does_not_interrupt_tree_cleanup(self):
        entered, cleaning, release, stopped = (asyncio.Event() for _ in range(4))
        async def work(context):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                stopped.set()
        self.runtime.spawn("root", execute=work, payload={})
        await entered.wait()
        finish = asyncio.create_task(self.runtime.finish_root("root", "completed"))
        await cleaning.wait()
        finish.cancel()
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(finish, return_exceptions=True)
        await self.runtime.finish_root("root", "interrupted")
        self.assertTrue(stopped.is_set())

    async def test_explicit_release_reclaims_context_but_preserves_request_identity(self):
        self.runtime.MAX_CONTEXT_BYTES = 180
        async def work(context):
            return "r" * 60
        child = self.runtime.spawn("root", execute=work, payload="p" * 60, request_id="first")
        await self.settled("root", [child["id"]])
        with self.assertRaises(RuntimeError):
            self.runtime.spawn("root", execute=work, payload="p" * 60)
        released = self.runtime.release("root", child["id"])
        self.assertTrue(released["context_released"])
        self.assertIsNone(released["result"])
        repeated = self.runtime.spawn("root", execute=work, payload="p" * 60, request_id="first")
        self.assertEqual(repeated["id"], child["id"])
        self.assertTrue(repeated["context_released"])
        with self.assertRaises(RuntimeError):
            self.runtime.send("root", child["id"], "resume")
        next_child = self.runtime.spawn("root", execute=work, payload="p" * 60)
        self.assertNotEqual(next_child["id"], child["id"])
        row = (await self.settled("root", [next_child["id"]]))["agents"][0]
        self.assertEqual(row["result"], "r" * 60)

    async def test_release_rejects_active_work_and_subtree_release_keeps_safe_diagnostics(self):
        release = asyncio.Event()
        async def work(context):
            await release.wait()
            return {"private": "result"}
        child = self.runtime.spawn("root", execute=work, payload={"private": "prompt"})
        with self.assertRaises(RuntimeError):
            self.runtime.release("root", child["id"])
        release.set()
        await self.settled("root", [child["id"]])
        self.runtime.release("root", child["id"])
        self.assertNotIn("private", str(self.runtime.snapshot()))
        self.assertNotIn("private", str(self.observed))

    async def test_observer_failure_does_not_change_result_and_root_deadline_cancels(self):
        def broken_observer(event):
            raise RuntimeError("private diagnostic failure")
        self.runtime.observer = broken_observer
        stopped = asyncio.Event()
        self.runtime.register_root("short", cancel_event=asyncio.Event(), deadline=time.monotonic() + .03)
        async def work(context):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.runtime.spawn("short", execute=work, payload={})
        await asyncio.wait_for(stopped.wait(), 1)
        self.assertNotIn("private", str(self.runtime.snapshot()))

    async def test_unconsumed_inbox_fails_instead_of_spinning_generations(self):
        self.runtime.register_root("brief", cancel_event=asyncio.Event(), deadline=time.monotonic() + .03)
        invocations = []
        async def ignores_messages(context):
            invocations.append(context.generation)
            return "stale answer"
        child = self.runtime.spawn("brief", execute=ignores_messages, payload={})
        self.runtime.send("brief", child["id"], "must be consumed")
        row = (await self.settled("brief", [child["id"]]))["agents"][0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "unconsumed_messages")
        self.assertEqual(invocations, [1])

    async def test_finish_during_root_cancel_does_not_cancel_child_cleanup_twice(self):
        entered, cleaning, release, stopped = (asyncio.Event() for _ in range(4))
        async def work(context):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                stopped.set()
        self.runtime.spawn("root", execute=work, payload={})
        await entered.wait()
        self.cancel_event.set()
        await cleaning.wait()
        finishing = asyncio.create_task(self.runtime.finish_root("root", "cancelled"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        await finishing
        self.assertTrue(stopped.is_set())

    async def test_execution_state_failure_is_failed_and_can_receive_retry(self):
        async def work(context):
            if context.generation == 1:
                raise RuntimeStateError("private execution failure")
            self.runtime.drain_inbox(context.id)
            return "retried"
        child = self.runtime.spawn("root", execute=work, payload={})
        row = (await self.settled("root", [child["id"]]))["agents"][0]
        self.assertEqual(row["status"], "failed")
        self.assertNotIn("private", str(row))
        self.runtime.send("root", child["id"], "try again")
        retried = (await self.settled("root", [child["id"]]))["agents"][0]
        self.assertEqual(retried["result"], "retried")

    async def test_cancelled_callback_cannot_publish_a_returned_late_result(self):
        entered = asyncio.Event()
        async def work(context):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return "late result must be discarded"
        child = self.runtime.spawn("root", execute=work, payload={})
        await entered.wait()
        row = await self.runtime.cancel("root", child["id"])
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(row["result"])

    async def test_cancel_before_followup_starts_reclaims_discarded_previous_result(self):
        self.runtime.MAX_CONTEXT_BYTES = 130
        async def work(context):
            return "x" * 100
        child = self.runtime.spawn("root", execute=work, payload={})
        await self.settled("root", [child["id"]])
        self.runtime.send("root", child["id"], "resume")
        await self.runtime.cancel("root", child["id"])
        async def next_work(context):
            return None
        next_child = self.runtime.spawn("root", execute=next_work, payload="new" * 10)
        row = (await self.settled("root", [next_child["id"]]))["agents"][0]
        self.assertEqual(row["status"], "completed")

    async def test_wait_reports_history_gap_instead_of_losing_an_old_message(self):
        release = asyncio.Event()
        async def hold(context):
            await release.wait()
            self.runtime.drain_inbox(context.id)
        async def quick(context):
            return None
        target = self.runtime.spawn("root", execute=hold, payload={})
        await asyncio.sleep(0)
        cursor = self.runtime.inspect("root", [target["id"]])["cursor"]
        self.runtime.send("root", target["id"], "pending instruction")
        for _ in range(175):
            other = self.runtime.spawn("root", execute=quick, payload={})
            await self.settled("root", [other["id"]])
        update = await self.runtime.wait("root", [target["id"]], after=cursor, timeout=.01)
        self.assertFalse(update["timed_out"])
        self.assertTrue(update.get("events_truncated"))
        release.set()

    async def test_root_finish_wakes_external_waiters_with_a_safe_lifecycle_error(self):
        async def work(context):
            await asyncio.Event().wait()
        child = self.runtime.spawn("root", execute=work, payload={})
        await asyncio.sleep(0)
        # Ignore all events: this waiter observes the root's disposal rather
        # than returning early for a child status transition during shutdown.
        waiter = asyncio.create_task(self.runtime.wait("root", [child["id"]], after=10_000, timeout=1))
        await asyncio.sleep(0)
        await self.runtime.finish_root("root", "completed")
        outcome = (await asyncio.gather(waiter, return_exceptions=True))[0]
        self.assertTrue(isinstance(outcome, RuntimeAccessError)
                        or isinstance(outcome, dict) and outcome["agents"][0]["status"] == "cancelled",
                        f"Expected a safe lifecycle outcome, received {type(outcome).__name__}")

    async def test_cancelled_cancel_waiter_still_reclaims_prestart_task_slot(self):
        self.runtime.max_active_per_root = 1
        child = None
        async def stop_child():
            await self.runtime.cancel("root", child["id"])
        async def work(context):
            return "done"
        stopping = asyncio.create_task(stop_child())
        child = self.runtime.spawn("root", execute=work, payload={})
        asyncio.get_running_loop().call_soon(stopping.cancel)
        await asyncio.gather(stopping, return_exceptions=True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(self.runtime.inspect("root", [child["id"]])["agents"][0]["status"], "cancelled")
        replacement = self.runtime.spawn("root", execute=work, payload={})
        row = (await self.settled("root", [replacement["id"]]))["agents"][0]
        self.assertEqual(row["result"], "done")
