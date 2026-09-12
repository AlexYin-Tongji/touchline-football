"""Real adapter wire requests against an in-process HTTP transport (no paid API)."""
import json
import unittest
from unittest.mock import patch

import httpx

from server import provider, agent
from server.agent_tools import OPENAI_TOOLS
from tests.test_agent import FakeStore


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = {"base_url": "https://example.test/v1", "model": "example", "api_key": "secret-do-not-echo",
                    "max_output_tokens": 3000, "_session_id": "test_session"}
        self.client = httpx.AsyncClient

    def client_patch(self, handler, kwargs_seen=None):
        def factory(**kwargs):
            if kwargs_seen is not None:
                kwargs_seen.append(kwargs)
            return self.client(transport=httpx.MockTransport(handler), **kwargs)
        return patch.object(provider.httpx, "AsyncClient", side_effect=factory)

    def test_endpoint_base_full_and_explicit_mismatch(self):
        for protocol, suffix in [("responses", "/responses"), ("chat_completions", "/chat/completions")]:
            for base, expected in [("https://example.test", "/v1" + suffix),
                                   ("https://example.test/custom/v1/", "/custom/v1" + suffix),
                                   ("https://example.test/v1" + suffix, "/v1" + suffix)]:
                self.assertEqual(provider.endpoint_url(dict(self.cfg, protocol=protocol, base_url=base)), "https://example.test" + expected)
        for raw in ["https://example.test/v1/responses", "https://user:secret@example.test/v1", "https://example.test/v1?key=secret"]:
            with self.assertRaises(provider.AgentFailure) as caught:
                provider.endpoint_url(dict(self.cfg, base_url=raw))
            self.assertNotIn("secret", str(caught.exception))

    async def test_chat_wire_and_private_reasoning_continuation(self):
        captured, options = [], []
        def handler(request):
            captured.append(json.loads(request.content))
            self.assertEqual(request.headers["user-agent"], "Touchline/2.0")
            self.assertEqual(request.headers["x-opencode-session"], "test_session")
            if len(captured) == 1:
                message = {"role": "assistant", "content": None, "reasoning_content": "private-chain",
                           "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "team_context", "arguments": "{}"}}]}
            else:
                message = {"role": "assistant", "content": "OK"}
            return httpx.Response(200, json={"choices": [{"message": message}]})
        cfg = dict(self.cfg, model="deepseek-v4-flash", use_env_proxy=False)
        messages = [{"role": "user", "content": "news"}]
        with self.client_patch(handler, options):
            first = await provider.request_completion(cfg, messages, tools=OPENAI_TOOLS)
            messages.extend([provider.assistant_context(first), {"role": "tool", "tool_call_id": "call_1", "content": "{}"}])
            final = await provider.request_completion(cfg, messages, tools=OPENAI_TOOLS)
        self.assertEqual(final["content"], "OK")
        self.assertEqual(captured[1]["messages"][1]["reasoning_content"], "private-chain")
        self.assertNotIn("temperature", captured[0])
        self.assertEqual(captured[0]["thinking"], {"type": "disabled"})
        self.assertEqual(captured[0]["max_tokens"], 3000)
        self.assertTrue(all(option["trust_env"] is False for option in options))
        self.assertNotIn("_chat_reasoning", json.dumps(captured))

    async def test_responses_returns_all_output_and_call_id_with_store_false(self):
        captured = []
        private = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "private-encrypted"}
        function = {"type": "function_call", "id": "fc_1", "call_id": "call_77", "name": "team_context", "arguments": "{}", "status": "completed"}
        def handler(request):
            self.assertEqual(request.url.path, "/v1/responses")
            captured.append(json.loads(request.content))
            output = [private, function] if len(captured) == 1 else [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}]
            return httpx.Response(200, json={"status": "completed", "output": output})
        cfg = dict(self.cfg, protocol="responses", model="o4-mini")
        messages = [{"role": "system", "content": "read"}, {"role": "user", "content": "test"}]
        with self.client_patch(handler):
            first = await provider.request_completion(cfg, messages, tools=OPENAI_TOOLS)
            messages.extend([provider.assistant_context(first), {"role": "tool", "tool_call_id": first["tool_calls"][0]["id"], "content": "{}"}])
            last = await provider.request_completion(cfg, messages, tools=OPENAI_TOOLS)
        self.assertEqual(last["content"], "done")
        self.assertEqual(captured[1]["input"][2:4], [private, function])
        self.assertEqual(captured[1]["input"][-1], {"type": "function_call_output", "call_id": "call_77", "output": "{}"})
        self.assertFalse(captured[0]["store"])
        self.assertEqual(captured[0]["include"], ["reasoning.encrypted_content"])
        self.assertEqual(captured[0]["max_output_tokens"], 3000)
        self.assertFalse(captured[0]["tools"][0]["strict"])
        self.assertNotIn("function", captured[0]["tools"][0])
        self.assertNotIn("messages", captured[0])

    def test_temperature_opt_in_and_reasoning_token_parameter(self):
        for model, parameter in [("o3", "max_completion_tokens"), ("gpt-5.6", "max_completion_tokens"), ("gpt-4.1", "max_tokens")]:
            result = provider.make_payload(dict(self.cfg, model=model), [])
            self.assertIn(parameter, result)
            self.assertNotIn("temperature", result)
        self.assertEqual(provider.make_payload(dict(self.cfg, send_temperature=True, temperature=0.4), [])["temperature"], 0.4)

    async def test_diagnostics_whitelist_and_no_protocol_fallback(self):
        seen = []
        def handler(request):
            seen.append(request.url.path)
            return httpx.Response(400, json={"error": {"code": "unsupported_parameter", "param": "temperature", "message": "leak secret-do-not-echo https://sensitive.test"}})
        with self.client_patch(handler), self.assertRaises(provider.AgentFailure) as caught:
            await provider.request_completion(self.cfg, [{"role": "user", "content": "OK"}])
        self.assertEqual(seen, ["/v1/chat/completions"])
        self.assertIn("temperature", str(caught.exception))
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("sensitive", str(caught.exception))

    async def test_truncated_reasoning_is_not_a_false_success(self):
        def handler(request):
            return httpx.Response(200, json={"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                                           "output": [{"type": "reasoning", "encrypted_content": "private"}]})
        with self.client_patch(handler), self.assertRaises(provider.AgentFailure) as caught:
            await provider.request_completion(dict(self.cfg, protocol="responses"), [])
        self.assertIn("令牌限制", str(caught.exception))

    async def test_three_stage_check_roundtrips_real_responses_wire(self):
        calls = []
        def handler(request):
            payload = json.loads(request.content)
            calls.append(payload)
            if len(calls) == 1:
                output = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]}]
            elif len(calls) == 2:
                self.assertEqual(payload["tool_choice"], {"type": "function", "name": "connection_probe"})
                output = [{"type": "function_call", "call_id": "probe", "name": "connection_probe", "arguments": json.dumps({"nonce": "touchline_connection_check"})}]
            else:
                result = json.loads(payload["input"][-1]["output"])["result"]
                output = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": result}]}]
            return httpx.Response(200, json={"output": output})
        with self.client_patch(handler):
            result = await agent.test_connection(dict(self.cfg, protocol="responses"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["tool_support"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(result["checks"]), 3)

    async def test_harness_never_persists_private_output_items(self):
        store = FakeStore()
        store.config["llm"]["protocol"] = "responses"
        store.new_run()
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            output = ([{"type": "reasoning", "encrypted_content": "PRIVATE_REASONING", "summary": []},
                       {"type": "function_call", "call_id": "evidence", "name": "read_evidence", "arguments": json.dumps({"evidence_id": "ev_injury"})}]
                      if len(seen) == 1 else [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "原文说明训练缺席。[ev_injury]"}]}])
            return httpx.Response(200, json={"output": output})
        with self.client_patch(handler):
            await agent.run_agent(store, "run_one", __import__("asyncio").Event())
        run = store.get("runs", "run_one")
        self.assertEqual(run["status"], "completed")
        self.assertIn("PRIVATE_REASONING", json.dumps(seen[1]))
        self.assertNotIn("PRIVATE_REASONING", json.dumps(run))
        self.assertNotIn("_response_output", json.dumps(run))
