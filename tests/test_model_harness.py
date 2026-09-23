"""Runtime multi-model scheduling contracts, using controllable local requests."""
import asyncio
import unittest
from collections import Counter
from unittest.mock import AsyncMock, patch

from server import agent
from server.model_harness import ModelHarness
from tests.test_agent import FakeStore, tool_message
from tests.test_verification import quoted_submission, checker_result


class ModelHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_roots_alternate_instead_of_one_tree_filling_the_queue(self):
        harness = ModelHarness(global_limit=1, provider_limit=1)
        entered, release = asyncio.Event(), asyncio.Event()
        order = []
        config = {"base_url": "https://provider.example/v1", "model": "test"}
        async def hold():
            entered.set()
            await release.wait()
        async def work(label):
            order.append(label)
        blocker = asyncio.create_task(harness.request(config, hold, owner_id="hold", role="chat", stage="answer"))
        await entered.wait()
        tasks = []
        for label, root in [("a1", "a"), ("a2", "a"), ("a3", "a"), ("b1", "b"), ("b2", "b")]:
            tasks.append(asyncio.create_task(harness.request(config, lambda label=label: work(label),
                owner_id=label, root_id=root, role="chat", stage="answer")))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(blocker, *tasks)
        self.assertEqual(order, ["a1", "b1", "a2", "b2", "a3"])

    async def test_blocked_provider_does_not_block_another_provider_in_same_root(self):
        harness = ModelHarness(global_limit=2, provider_limit=1)
        entered, release, independent = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def hold():
            entered.set()
            await release.wait()
        first = asyncio.create_task(harness.request({"base_url": "https://a.example/v1"}, hold,
            owner_id="a1", root_id="root", role="chat", stage="answer"))
        await entered.wait()
        queued = asyncio.create_task(harness.request({"base_url": "https://a.example/another"}, hold,
            owner_id="a2", root_id="root", role="chat", stage="answer"))
        async def independent_work():
            independent.set()
        other = asyncio.create_task(harness.request({"base_url": "https://b.example/v1"}, independent_work,
            owner_id="b", root_id="root", role="verify", stage="verify"))
        try:
            await asyncio.wait_for(independent.wait(), 1)
        finally:
            release.set()
            await asyncio.gather(first, queued, other)
        self.assertFalse(harness.snapshot()["active"])

    async def test_cancel_after_admission_and_execution_failure_release_capacity(self):
        harness = ModelHarness(global_limit=1, provider_limit=1)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        config = {"base_url": "https://a.example/v1"}
        async def hold():
            entered.set()
            await release.wait()
            asyncio.get_running_loop().call_soon(second.cancel)
        async def unwanted():
            calls.append("cancelled request executed")
        first = asyncio.create_task(harness.request(config, hold, owner_id="hold", root_id="a", role="chat", stage="answer"))
        await entered.wait()
        second = asyncio.create_task(harness.request(config, unwanted, owner_id="cancel", root_id="b", role="chat", stage="answer"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual(calls, [])
        async def failing():
            raise ValueError("private failure")
        with self.assertRaises(ValueError):
            await harness.request(config, failing, owner_id="bad", root_id="b", role="chat", stage="answer")
        async def successful():
            return "done"
        result = await asyncio.wait_for(harness.request(config, successful, owner_id="good", root_id="b", role="chat", stage="answer"), 1)
        self.assertEqual(result, "done")
        self.assertNotIn("private failure", str(harness.snapshot()))
        self.assertFalse(harness.snapshot()["active"])

    async def test_answer_and_verification_use_assigned_models_with_separate_context(self):
        store = FakeStore()
        store.config["verification_enabled"] = True
        metadata = {"model_roles": {"verify": {"provider_id": "reviewer", "model": "check"}}}
        store.get_meta = lambda key: metadata.get(key)
        store.get_secret = lambda key: "synthetic-" + key
        for identifier, model in (("writer", "write"), ("reviewer", "check")):
            store.put("model_profiles", {"id": identifier, "base_url": f"https://{identifier}.example/v1",
                                         "protocol": "chat_completions", "models": [{"id": model}]})
        store.new_run(provider_id="writer", model="write", model_role="chat")
        responses = [tool_message("read_evidence", {"evidence_id": "ev_injury"}),
                     tool_message("submit_answer", quoted_submission()), checker_result()]
        async def respond(*args, **kwargs):
            response = responses.pop(0)
            # Settings changed while drafting must affect only later tasks.
            if len(responses) == 1:
                metadata["model_roles"]["verify"] = {"provider_id": "writer", "model": "write"}
            return response
        requester = AsyncMock(side_effect=respond)
        with patch("server.agent.request_completion", requester):
            await agent.run_agent(store, "run_one", asyncio.Event())
        run = store.get("runs", "run_one")
        self.assertEqual(run["verification"]["status"], "supported")
        self.assertEqual([call.args[0]["model"] for call in requester.call_args_list], ["write", "write", "check"])
        self.assertEqual([message["role"] for message in requester.call_args_list[-1].args[1]], ["system", "user"])
        self.assertEqual(requester.call_args_list[-1].args[0]["api_key"], "synthetic-provider_reviewer")
        self.assertEqual(run["verification_model"]["model"], "check")
        self.assertNotIn("synthetic-provider", str(store._model_harness.snapshot()))

    async def test_independent_models_overlap_with_global_and_provider_limits(self):
        harness = ModelHarness(global_limit=3, provider_limit=2)
        active, peaks = Counter(), Counter()
        ready, release = asyncio.Event(), asyncio.Event()
        async def work(provider):
            active[provider] += 1
            active["total"] += 1
            peaks[provider] = max(peaks[provider], active[provider])
            peaks["total"] = max(peaks["total"], active["total"])
            if active["total"] == 3:
                ready.set()
            try:
                await release.wait()
                return provider
            finally:
                active[provider] -= 1
                active["total"] -= 1
        tasks = [asyncio.create_task(harness.request(
            {"base_url": f"https://{provider}.example/v1", "model": "model-" + provider},
            lambda p=provider: work(p), owner_id=f"job-{index}", role="feed", stage="draft"))
            for index, provider in enumerate(("a", "a", "a", "b", "b"))]
        try:
            await asyncio.wait_for(ready.wait(), 1)
            self.assertEqual(peaks["total"], 3)
            self.assertLessEqual(peaks["a"], 2)
            release.set()
            self.assertEqual(await asyncio.gather(*tasks), ["a", "a", "a", "b", "b"])
            self.assertFalse(harness.snapshot()["active"])
            self.assertEqual(len(harness.snapshot()["recent"]), 5)
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_cancelled_queued_job_never_calls_model_or_leaks_credentials(self):
        harness = ModelHarness(global_limit=1, provider_limit=1)
        entered, release = asyncio.Event(), asyncio.Event()
        async def hold():
            entered.set()
            await release.wait()
        config = {"base_url": "https://provider.example/v1", "model": "test", "api_key": "never-expose"}
        first = asyncio.create_task(harness.request(config, hold, owner_id="first", role="feed", stage="draft"))
        await entered.wait()
        invoked = False
        async def queued_work():
            nonlocal invoked
            invoked = True
        second = asyncio.create_task(harness.request(config, queued_work, owner_id="second", role="verify", stage="verify"))
        await asyncio.sleep(0)
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        release.set()
        await first
        self.assertFalse(invoked)
        snapshot = harness.snapshot()
        self.assertNotIn("never-expose", str(snapshot))
        self.assertFalse(snapshot["active"])
        self.assertEqual(next(row["status"] for row in snapshot["recent"] if row["owner_id"] == "second"), "cancelled")
