"""Anthropic Messages and three-protocol tool continuation on local transports."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import httpx

from server import agent, provider
from tests.test_agent import FakeStore


TOOL = {"type": "function", "function": {"name": "team_context", "description": "Read source-backed team data", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}


class AnthropicProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = {"base_url": "https://api.example/v1", "model": "claude-example", "protocol": "anthropic_messages",
                       "api_key": "synthetic-secret", "max_output_tokens": 2048, "use_env_proxy": False}
        self.http_client = httpx.AsyncClient

    def network(self, handler):
        return patch.object(provider.httpx, "AsyncClient", side_effect=lambda **kwargs: self.http_client(transport=httpx.MockTransport(handler), **kwargs))

    def test_anthropic_endpoint_and_system_messages(self):
        for base in ("https://api.example", "https://api.example/v1", "https://api.example/v1/messages"):
            self.assertEqual(provider.endpoint_url(dict(self.config, base_url=base)), "https://api.example/v1/messages")
        with self.assertRaises(provider.AgentFailure):
            provider.endpoint_url(dict(self.config, base_url="https://api.example/v1/responses"))
        payload = provider.make_payload(self.config, [{"role": "system", "content": "one"}, {"role": "developer", "content": "two"}, {"role": "user", "content": "question"}])
        self.assertEqual(payload["system"], "one\n\ntwo")
        self.assertEqual(payload["messages"], [{"role": "user", "content": [{"type": "text", "text": "question"}]}])
        self.assertEqual(payload["max_tokens"], 2048)
        self.assertNotIn("temperature", payload)
        self.assertNotIn("thinking", payload)

    def test_tool_choices_are_translated_including_none(self):
        for choice, expected in (("auto", {"type": "auto"}), ("required", {"type": "any"}), ("none", {"type": "none"}),
                                 ({"type": "function", "function": {"name": "team_context"}}, {"type": "tool", "name": "team_context"})):
            with self.subTest(choice=choice):
                payload = provider.make_payload(self.config, [{"role": "user", "content": "read"}], [TOOL], choice)
                self.assertEqual(payload.get("tool_choice"), expected)
                self.assertEqual(payload["tools"][0]["input_schema"], TOOL["function"]["parameters"])

    async def test_tool_use_result_continuation_keeps_definitions_when_disabled(self):
        captured = []
        private = {"type": "thinking", "thinking": "PRIVATE-THINKING", "signature": "signed-block"}
        use = {"type": "tool_use", "id": "toolu_1", "name": "team_context", "input": {}}
        def handler(request):
            self.assertEqual(request.url.path, "/v1/messages")
            self.assertEqual(request.headers["x-api-key"], "synthetic-secret")
            self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
            self.assertNotIn("authorization", request.headers)
            payload = json.loads(request.content)
            captured.append(payload)
            if len(captured) == 1:
                return httpx.Response(200, json={"content": [private, use], "stop_reason": "tool_use"})
            self.assertTrue(payload.get("tools"), "Anthropic requires tool definitions while replaying tool_use history")
            self.assertEqual(payload.get("tool_choice"), {"type": "none"})
            self.assertEqual(payload["messages"][-1]["content"][0]["tool_use_id"], "toolu_1")
            return httpx.Response(200, json={"content": [{"type": "text", "text": "中文结论"}], "stop_reason": "end_turn"})
        messages = [{"role": "user", "content": "球队情况"}]
        with self.network(handler):
            first = await provider.request_completion(self.config, messages, tools=[TOOL])
            self.assertIsNone(first["content"])
            messages.extend([provider.assistant_context(first), {"role": "tool", "tool_call_id": "toolu_1", "content": '{"players":27}'}])
            result = await provider.request_completion(self.config, messages, tools=[TOOL], tool_choice="none")
        self.assertEqual(result["content"], "中文结论")
        self.assertEqual(captured[1]["messages"][1]["content"], [private, use])
        self.assertNotIn("_anthropic_content", json.dumps(captured))

    def test_parallel_tool_results_are_one_adjacent_user_message(self):
        calls = [{"type": "tool_use", "id": f"call_{i}", "name": "team_context", "input": {}} for i in (1, 2)]
        message = provider.response_message({"content": calls, "stop_reason": "tool_use"}, "anthropic_messages")
        messages = [{"role": "user", "content": "read"}, provider.assistant_context(message),
                    {"role": "tool", "tool_call_id": "call_1", "content": "first"}, {"role": "tool", "tool_call_id": "call_2", "content": "second"}]
        payload = provider.make_payload(self.config, messages, [TOOL])
        self.assertEqual(len(payload["messages"]), 3)
        self.assertEqual([p["tool_use_id"] for p in payload["messages"][-1]["content"]], ["call_1", "call_2"])

    def test_wire_conversion_does_not_mutate_saved_continuation_blocks(self):
        messages = [{"role": "user", "content": "read"},
                    {"role": "assistant", "content": "first", "_anthropic_content": [{"type": "text", "text": "first"}]},
                    {"role": "assistant", "content": "second"}]
        before = copy.deepcopy(messages)
        first = provider.make_payload(self.config, messages)
        second = provider.make_payload(self.config, messages)
        self.assertEqual(messages, before)
        self.assertEqual(first, second)

    async def test_truncated_or_malformed_responses_fail_safely(self):
        for response in ({"stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}]},
                         {"content": [{"type": "tool_use", "name": "missing-id", "input": {}}]},
                         {"content": [{"type": "thinking", "thinking": "private-only"}]}, {"content": None}):
            with self.subTest(response=response), self.network(lambda r: httpx.Response(200, json=response)), self.assertRaises(provider.AgentFailure) as caught:
                await provider.request_completion(self.config, [{"role": "user", "content": "read"}])
            self.assertTrue(caught.exception.response_received)
            self.assertNotIn("private-only", str(caught.exception))

    async def test_agent_keeps_signed_thinking_in_memory_and_out_of_saved_run(self):
        store = FakeStore()
        store.config["llm"]["protocol"] = "anthropic_messages"
        store.new_run()
        captured = []
        def handler(request):
            captured.append(json.loads(request.content))
            content = ([{"type": "thinking", "thinking": "PRIVATE-CHAIN-ONLY", "signature": "PRIVATE-SIGNATURE-ONLY"},
                        {"type": "tool_use", "id": "evidence_1", "name": "read_evidence", "input": {"evidence_id": "ev_injury"}}]
                       if len(captured) == 1 else [{"type": "text", "text": "原文说明训练缺席。[ev_injury]"}])
            return httpx.Response(200, json={"content": content, "stop_reason": "tool_use" if len(captured) == 1 else "end_turn"})
        with self.network(handler):
            await agent.run_agent(store, "run_one", asyncio.Event())
        run = store.get("runs", "run_one")
        self.assertEqual(run["status"], "completed", run.get("error"))
        self.assertIn("PRIVATE-SIGNATURE-ONLY", json.dumps(captured[1]))
        self.assertNotIn("PRIVATE-CHAIN-ONLY", json.dumps(run))
        self.assertNotIn("PRIVATE-SIGNATURE-ONLY", json.dumps(run))
        self.assertNotIn("_anthropic_content", json.dumps(run))

    async def test_three_stage_connection_check_roundtrips_all_three_protocols(self):
        for protocol in ("chat_completions", "responses", "anthropic_messages"):
            captured = []
            def handler(request):
                payload = json.loads(request.content)
                captured.append(payload)
                stage = len(captured)
                if protocol == "anthropic_messages":
                    if stage == 2:
                        self.assertEqual(payload["tool_choice"], {"type": "tool", "name": "connection_probe"})
                        content = [{"type": "tool_use", "id": "probe_1", "name": "connection_probe", "input": {"nonce": "touchline_connection_check"}}]
                    else:
                        if stage == 3:
                            self.assertTrue(payload.get("tools"))
                            self.assertEqual(payload["tool_choice"], {"type": "none"})
                        text = "OK" if stage == 1 else json.loads(payload["messages"][-1]["content"][0]["content"])["result"]
                        content = [{"type": "text", "text": text}]
                    return httpx.Response(200, json={"content": content, "stop_reason": "tool_use" if stage == 2 else "end_turn"})
                if protocol == "responses":
                    if stage == 2:
                        output = [{"type": "function_call", "call_id": "probe_1", "name": "connection_probe", "arguments": '{"nonce":"touchline_connection_check"}'}]
                    else:
                        text = "OK" if stage == 1 else json.loads(payload["input"][-1]["output"])["result"]
                        output = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]
                    return httpx.Response(200, json={"status": "completed", "output": output})
                if stage == 2:
                    message = {"role": "assistant", "content": None, "tool_calls": [{"id": "probe_1", "type": "function", "function": {"name": "connection_probe", "arguments": '{"nonce":"touchline_connection_check"}'}}]}
                else:
                    text = "OK" if stage == 1 else json.loads(payload["messages"][-1]["content"])["result"]
                    message = {"role": "assistant", "content": text}
                return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "tool_calls" if stage == 2 else "stop"}]})
            with self.subTest(protocol=protocol), self.network(handler):
                result = await agent.test_connection(dict(self.config, protocol=protocol))
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["tool_support"])
                self.assertEqual(len(captured), 3)


if __name__ == "__main__":
    unittest.main()
