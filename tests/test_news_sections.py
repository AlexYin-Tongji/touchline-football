"""Bounded section agents use only already-read source passages and no real models."""
import asyncio
import copy
import json
import time
import unittest
from types import SimpleNamespace

from server.agent_runtime import AgentRuntime
from server.agent_tools import LocalTools
from tests.test_agent import FakeStore, tool_message


class NewsSectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = FakeStore()
        self.run = self.store.new_run(question="PARENT_CONVERSATION_SECRET")
        self.local = LocalTools(self.store, self.run, self.store.settings())
        self.observed = []
        self.runtime = AgentRuntime(observer=self.observed.append)
        self.cancel = asyncio.Event()
        self.runtime.register_root("section_parent", cancel_event=self.cancel, deadline=time.monotonic() + 10)
        self.delegation = SimpleNamespace(runtime=self.runtime, id="section_parent")
        self.config = {"model": "test", "_session_id": "parent_session"}

    async def asyncTearDown(self):
        await self.runtime.shutdown()

    def read_source(self, count=24, identity="ev_injury"):
        paragraphs = [f"Paragraph {index:03d}: " + (f"Specific fact {index}, date 19 September 2026, conditional quotation. " * 12)[:580]
                      for index in range(count)]
        source = {**self.store.get("evidence", "ev_injury"), "id": identity, "text": "\n\n".join(paragraphs)}
        self.store.put("evidence", source)
        offset = 0
        while True:
            page = self.local.call("read_evidence", {"evidence_id": identity, "offset": offset})
            if page.get("next_offset") is None:
                break
            offset = page["next_offset"]
        return self.local.read_cache[identity]

    @staticmethod
    def response(payload, ids=None):
        return tool_message("submit_section_notes", {"notes": [{"text": "原文明确提供人物、时间及附带条件，归因仍保留为该来源说法。",
            "passage_ids": ids if ids is not None else [p["id"] for p in payload["core_passages"]]}]})

    async def build(self, requester, **kwargs):
        from server.news_sections import build_section_notes
        return await build_section_notes(self.local, self.config, self.delegation, requester=requester, **kwargs)

    async def test_no_read_sources_do_not_spawn_or_call_models(self):
        async def unexpected(*args, **kwargs):
            self.fail("No source must mean no model request")
        result = await self.build(unexpected)
        self.assertEqual(result, {"sections": [], "errors": [], "total_sections": 0})
        self.assertEqual(self.runtime.inspect(self.delegation.id)["agents"], [])

    async def test_sections_keep_all_passages_order_and_at_most_three_observable_children(self):
        cache = self.read_source(48)
        original = copy.deepcopy(cache)
        payloads, sessions, options = [], [], []
        entered, release = asyncio.Event(), asyncio.Event()
        active = peak = 0
        async def requester(config, messages, **kwargs):
            nonlocal active, peak
            payload = json.loads(messages[1]["content"])
            self.assertEqual([m["role"] for m in messages], ["system", "user"])
            self.assertNotIn("PARENT_CONVERSATION_SECRET", json.dumps(messages))
            self.assertEqual(kwargs["stage"], "section")
            self.assertEqual(kwargs["role"], "feed")
            self.assertEqual(config["_session_id"], kwargs["owner_id"])
            self.assertEqual(kwargs["tool_choice"]["function"]["name"], "submit_section_notes")
            self.assertEqual([tool["function"]["name"] for tool in kwargs["tools"]], ["submit_section_notes"])
            payloads.append(payload); sessions.append(config["_session_id"]); options.append(kwargs)
            active += 1; peak = max(peak, active)
            if active == 3:
                entered.set()
            try:
                await release.wait()
                await asyncio.sleep(.002 * (2 - payload["order"] % 3))
                return self.response(payload)
            finally:
                active -= 1
        task = asyncio.create_task(self.build(requester))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            children = self.runtime.inspect(self.delegation.id)["agents"]
            self.assertEqual(len(children), 3)
            self.assertTrue(all(row["status"] == "running" for row in children))
            release.set()
            result = await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        payloads.sort(key=lambda p: p["order"])
        self.assertGreater(result["total_sections"], 3)
        self.assertEqual(result["errors"], [])
        self.assertEqual(peak, 3)
        self.assertEqual(len(set(sessions)), len(sessions))
        self.assertEqual([row["order"] for row in result["sections"]], list(range(result["total_sections"])))
        self.assertEqual([p["id"] for chunk in payloads for p in chunk["core_passages"]], [p["id"] for p in cache["passages"]])
        for chunk in payloads:
            length = sum(len(p["text"]) for p in chunk["core_passages"])
            self.assertLessEqual(length, 6000)
            if chunk is not payloads[-1]:
                self.assertGreaterEqual(length, 4500)
            self.assertLessEqual(len(chunk["context_passages"]), 1)
            self.assertFalse({p["id"] for p in chunk["core_passages"]} & {p["id"] for p in chunk["context_passages"]})
            self.assertTrue(all(len(p["text"]) <= 700 for p in chunk["core_passages"]))
        self.assertEqual(self.local.read_cache["ev_injury"], original)
        self.assertTrue(all(row["context_released"] and row["result"] is None for row in self.runtime.inspect(self.delegation.id)["agents"]))
        self.assertNotIn("Specific fact", json.dumps(self.observed))
        self.assertNotIn("原文明确提供", json.dumps(self.observed, ensure_ascii=False))
        self.assertEqual(self.config["_session_id"], "parent_session")

    async def test_forged_or_context_only_citations_fail_that_section_without_losing_siblings(self):
        self.read_source(30)
        async def requester(config, messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            if payload["order"] == 0:
                return self.response(payload, [payload["context_passages"][0]["id"]])
            if payload["order"] == 1:
                return self.response(payload, ["invented_passage"])
            return self.response(payload)
        result = await self.build(requester)
        self.assertGreaterEqual(result["total_sections"], 3)
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual(len(result["sections"]), result["total_sections"] - 2)
        self.assertTrue(all(row["order"] >= 2 for row in result["sections"]))
        self.assertEqual([r["status"] for r in self.runtime.inspect(self.delegation.id)["agents"]][:2], ["failed", "failed"])

    async def test_uncovered_passages_are_explicit_and_not_silently_dropped(self):
        cache = self.read_source(3)
        async def requester(config, messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            return self.response(payload, [payload["core_passages"][0]["id"]])
        result = await self.build(requester)
        self.assertEqual(len(result["sections"]), 1)
        self.assertEqual(result["sections"][0]["uncovered_passage_ids"], [p["id"] for p in cache["passages"]][1:])
        self.assertEqual(len(result["errors"]), 1)

    async def test_source_mutation_discards_its_notes(self):
        self.read_source(3)
        async def requester(config, messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            source = self.store.get("evidence", "ev_injury")
            self.store.put("evidence", {**source, "text": "Changed source, no longer the text that was read."})
            return self.response(payload)
        result = await self.build(requester)
        self.assertEqual(result["sections"], [])
        self.assertEqual(len(result["errors"]), 1)

    async def test_timeout_keeps_successful_sections_and_releases_every_child(self):
        self.read_source(20)
        cleaned = asyncio.Event()
        async def requester(config, messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            if payload["order"] == 0:
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
            return self.response(payload)
        result = await self.build(requester, timeout=.03)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(len(result["sections"]), result["total_sections"] - 1)
        self.assertEqual(len(result["errors"]), 1)
        children = self.runtime.inspect(self.delegation.id)["agents"]
        self.assertTrue(all(row["context_released"] for row in children))
        self.assertEqual(children[0]["status"], "failed")

    async def test_parent_cancellation_waits_for_child_cleanup_and_releases_context(self):
        self.read_source(30)
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        active = stopping = 0
        async def requester(config, messages, **kwargs):
            nonlocal active, stopping
            active += 1
            if active == 3:
                entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopping += 1
                if stopping == 3:
                    cleaning.set()
                await release.wait()
                active -= 1
        task = asyncio.create_task(self.build(requester))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.wait_for(cleaning.wait(), .2)
            self.assertFalse(task.done())
            task.cancel()  # A second cancelled waiter cannot abandon cleanup.
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(active, 0)
            children = self.runtime.inspect(self.delegation.id)["agents"]
            self.assertTrue(all(row["status"] == "cancelled" and row["context_released"] for row in children))
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_root_stop_cancels_section_requests_without_returning_partial_success(self):
        self.read_source(20)
        entered = asyncio.Event()
        cleaned = []
        async def requester(config, messages, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(kwargs["owner_id"])
        task = asyncio.create_task(self.build(requester))
        await asyncio.wait_for(entered.wait(), 1)
        self.cancel.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertTrue(cleaned)
        self.assertTrue(all(row["status"] == "cancelled" for row in self.runtime.inspect(self.delegation.id)["agents"]))
        await self.runtime.finish_root(self.delegation.id, "cancelled")
        self.assertEqual(self.runtime.snapshot()["active"], [])

    async def test_out_of_order_pages_never_introduce_unread_text_or_passages(self):
        source = self.store.get("evidence", "ev_injury")
        source["text"] = "\n\n".join(f"Part {i}: " + (str(i) + " read source detail. ") * 25 for i in range(20))
        self.store.put("evidence", source)
        self.local.call("read_evidence", {"evidence_id": "ev_injury", "offset": 6000, "length": 1200})
        self.local.call("read_evidence", {"evidence_id": "ev_injury", "offset": 0, "length": 1200})
        supplied = []
        async def requester(config, messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            supplied.extend(payload["core_passages"])
            return self.response(payload)
        result = await self.build(requester)
        cache = self.local.read_cache["ev_injury"]
        self.assertEqual(result["errors"], [])
        self.assertEqual({p["id"] for p in supplied}, {p["id"] for p in cache["passages"]})
        positions = [cache["text"].index(p["text"]) for p in supplied]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(all("未读取的正文区间" not in p["text"] for p in supplied))

    async def test_non_chinese_or_malformed_notes_do_not_become_candidates(self):
        self.read_source(3)
        for value in ("not json", json.dumps({"notes": []}), json.dumps({"notes": [{"text": "English only", "passage_ids": [self.local.read_cache["ev_injury"]["passages"][0]["id"]]}]})):
            with self.subTest(value=value):
                async def requester(config, messages, **kwargs):
                    return {"tool_calls": [{"function": {"name": "submit_section_notes", "arguments": value}}]}
                result = await self.build(requester)
                self.assertEqual(result["sections"], [])
                self.assertEqual(len(result["errors"]), 1)

    async def test_runtime_capacity_smaller_than_three_still_processes_all_sections(self):
        self.runtime.max_active_per_root = 1
        self.read_source(20)
        async def requester(config, messages, **kwargs):
            return self.response(json.loads(messages[1]["content"]))
        result = await self.build(requester)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["sections"]), result["total_sections"])
