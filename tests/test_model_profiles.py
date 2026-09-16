"""Credential isolation and model selection regressions; no external API calls."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from server import model_config
from server.provider import AgentFailure
from server.storage import Store


SECRET = "synthetic-key-for-tests-only"


def profile(**overrides):
    value = {"id": "alpha", "name": "Alpha", "base_url": "https://alpha.example/v1",
             "protocol": "chat_completions", "api_key": SECRET,
             "models": [{"id": "one", "name": "One", "max_output_tokens": 4096}, {"id": "two", "name": "Two"}]}
    value.update(overrides)
    return model_config.ProviderInput(**value)


class ModelProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "models.sqlite3")
        self.http_client = httpx.AsyncClient

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def client(self):
        app = FastAPI()
        model_config.register_routes(app, self.store)
        return TestClient(app)

    def mock_network(self, handler, options=None):
        def factory(**kwargs):
            if options is not None:
                options.append(kwargs)
            return self.http_client(transport=httpx.MockTransport(handler), **kwargs)
        return patch.object(model_config.httpx, "AsyncClient", side_effect=factory)

    def test_legacy_migration_is_idempotent_and_preserves_encrypted_key(self):
        self.store.save_settings({"llm": {"base_url": "https://legacy.example/v1", "model": "legacy-model", "api_key": SECRET, "protocol": "responses", "max_output_tokens": 5000}})
        model_config.migrate_legacy(self.store)
        model_config.migrate_legacy(self.store)
        view = model_config.profiles_view(self.store)
        self.assertEqual(len(view["providers"]), 1)
        self.assertEqual(view["active"], {"provider_id": "existing", "model": "legacy-model"})
        config = model_config.effective_config(self.store, "existing", "legacy-model")
        self.assertEqual((config["api_key"], config["max_output_tokens"], config["protocol"]), (SECRET, 5000, "responses"))
        self.assertEqual(self.store.settings()["llm"]["api_key"], SECRET)
        self.assertNotIn(SECRET, json.dumps(view))
        self.assertNotIn(SECRET, json.dumps(self.store.list("model_profiles")))
        secret_rows = self.store._db.execute("SELECT encrypted FROM secrets").fetchall()
        self.assertTrue(secret_rows)
        self.assertTrue(all(SECRET.encode() not in row[0] for row in secret_rows))

    def test_existing_catalog_is_not_overwritten_by_legacy_migration(self):
        model_config.save_profile(self.store, profile())
        self.store.save_settings({"llm": {"base_url": "https://legacy.example/v1", "model": "legacy", "api_key": "legacy-only"}})
        model_config.migrate_legacy(self.store)
        self.assertEqual([p["id"] for p in self.store.list("model_profiles")], ["alpha"])
        self.assertEqual(self.store.get_secret("provider_alpha"), SECRET)

    def test_provider_id_is_immutable_and_create_is_not_an_upsert(self):
        with self.client() as client:
            self.assertEqual(client.post("/api/model-providers", json=profile().model_dump()).status_code, 200)
            self.assertEqual(client.post("/api/model-providers", json=profile(name="Changed").model_dump()).status_code, 409)
            changed = profile(id="renamed").model_dump()
            self.assertEqual(client.put("/api/model-providers/alpha", json=changed).status_code, 400)
        self.assertIsNone(self.store.get("model_profiles", "renamed"))
        self.assertEqual(self.store.get("model_profiles", "alpha")["name"], "Alpha")
        self.assertEqual(self.store.get_secret("provider_alpha"), SECRET)

    def test_custom_provider_uses_automatic_id_and_accepts_chinese_display_name(self):
        payload = profile(name="我的本地模型", base_url="http://localhost:11434/v1", api_key=None,
                          models=[{"id": "Qwen/Model.V4:latest", "name": "中文模型"}]).model_dump()
        payload.pop("id")
        with self.client() as client:
            response = client.post("/api/model-providers", json=payload)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        identifier = result["saved_provider_id"]
        self.assertRegex(identifier, r"^[a-z][a-z0-9_-]{0,47}$")
        saved = self.store.get("model_profiles", identifier)
        self.assertEqual(saved["name"], "我的本地模型")
        self.assertEqual(saved["models"][0]["id"], "Qwen/Model.V4:latest")
        self.assertEqual(result["active"], {"provider_id": identifier, "model": "Qwen/Model.V4:latest"})
        self.assertFalse(result["providers"][0]["api_key_set"])

    def test_repeated_preset_creates_independent_profiles_and_credentials(self):
        payload = profile(name="DeepSeek", base_url="https://api.deepseek.com/v1").model_dump()
        payload.pop("id")
        with self.client() as client:
            first = client.post("/api/model-providers", json=payload)
            second = client.post("/api/model-providers", json={**payload, "api_key": "separate-test-key"})
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        first_id, second_id = first.json()["saved_provider_id"], second.json()["saved_provider_id"]
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(len(second.json()["providers"]), 2)
        self.assertEqual(self.store.get_secret("provider_" + first_id), SECRET)
        self.assertEqual(self.store.get_secret("provider_" + second_id), "separate-test-key")
        self.assertNotIn(SECRET, second.text)
        self.assertNotIn("separate-test-key", second.text)

    def test_display_name_edit_keeps_automatic_id_and_saved_credentials(self):
        payload = profile(name="中文服务商").model_dump()
        payload.pop("id")
        with self.client() as client:
            result = client.post("/api/model-providers", json=payload).json()
            identifier = result["saved_provider_id"]
            saved = result["providers"][0]
            edit = {**payload, "id": identifier, "name": "改名后的服务商", "api_key": None}
            response = client.put("/api/model-providers/" + identifier, json=edit)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["providers"][0]["id"], saved["id"])
        self.assertEqual(self.store.get_secret("provider_" + identifier), SECRET)

    def test_invalid_provider_id_has_chinese_diagnostic_without_sensitive_input(self):
        # Exercise the production validation handler, which deliberately omits
        # request inputs (including credentials) from its response.
        from server.main import create_app
        app = create_app(Path(self.directory.name) / "validation.sqlite3")
        client = TestClient(app)
        payload = profile().model_dump()
        invalid_secret = "Secret-" + SECRET
        try:
            for identifier in ("DeepSeek", "123model", "中文名称", "", "provider name", "a" * 49, invalid_secret):
                with self.subTest(identifier=identifier if identifier != invalid_secret else "sensitive"):
                    response = client.post("/api/model-providers", json={**payload, "id": identifier})
                    self.assertEqual(response.status_code, 422)
                    self.assertIn("服务商内部标识须以小写英文字母开头", response.json()["detail"])
                    self.assertNotIn("String should match pattern", response.text)
                    self.assertNotIn(SECRET, response.text)
            self.assertEqual(app.state.store.list("model_profiles"), [])
        finally:
            client.close()
            app.state.store.close()

    def test_same_origin_edit_preserves_key_but_cross_origin_edit_clears_it(self):
        model_config.save_profile(self.store, profile())
        model_config.save_profile(self.store, profile(base_url="https://alpha.example:443/custom/v1", api_key=None), update=True)
        self.assertEqual(self.store.get_secret("provider_alpha"), SECRET)
        result = model_config.save_profile(self.store, profile(base_url="https://beta.example/v1", api_key=None), update=True)
        self.assertFalse(result["providers"][0]["api_key_set"])
        self.assertEqual(self.store.get_secret("provider_alpha"), "")
        self.assertEqual(self.store.settings()["llm"]["api_key"], "")

    def test_origin_change_with_explicit_replacement_key_uses_only_new_key(self):
        model_config.save_profile(self.store, profile())
        model_config.save_profile(self.store, profile(base_url="https://beta.example/v1", api_key="replacement-only"), update=True)
        self.assertEqual(self.store.get_secret("provider_alpha"), "replacement-only")
        self.assertEqual(self.store.settings()["llm"]["api_key"], "replacement-only")

    def test_keyless_same_origin_provider_never_inherits_another_profile_key(self):
        model_config.save_profile(self.store, profile())
        model_config.save_profile(self.store, profile(id="keyless", api_key=None))
        model_config.select_model(self.store, "keyless", "one")
        self.assertEqual(self.store.settings()["llm"]["api_key"], "")
        self.assertEqual(self.store.get_secret("provider_alpha"), SECRET)

    def test_removing_selected_model_selects_remaining_model_in_same_provider(self):
        model_config.save_profile(self.store, profile())
        model_config.select_model(self.store, "alpha", "two")
        result = model_config.save_profile(self.store, profile(api_key=None, models=[{"id": "one", "name": "One"}]), update=True)
        self.assertEqual(result["active"], {"provider_id": "alpha", "model": "one"})
        self.assertEqual(self.store.settings()["llm"]["model"], "one")

    def test_delete_inactive_provider_preserves_active_then_active_delete_clears_runtime(self):
        with self.client() as client:
            client.post("/api/model-providers", json=profile().model_dump())
            client.post("/api/model-providers", json=profile(id="beta", base_url="https://beta.example/v1", api_key="beta-key").model_dump())
            self.assertEqual(client.delete("/api/model-providers/beta").status_code, 200)
            self.assertEqual(self.store.settings()["llm"]["api_key"], SECRET)
            response = client.delete("/api/model-providers/alpha")
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()["active"])
            self.assertEqual(client.delete("/api/model-providers/alpha").status_code, 404)
        self.assertEqual(self.store.get_secret("provider_alpha"), "")
        self.assertEqual(self.store.settings()["llm"]["api_key"], "")
        self.assertEqual(self.store.settings()["llm"]["model"], "")
        self.assertEqual(self.store.settings()["llm"]["base_url"], "")

    def test_run_uses_its_bound_provider_after_global_selection_changes(self):
        model_config.save_profile(self.store, profile())
        model_config.save_profile(self.store, profile(id="beta", base_url="https://beta.example/v1", protocol="anthropic_messages", api_key="beta-key", models=[{"id": "b-model"}]))
        model_config.select_model(self.store, "beta", "b-model")
        config = model_config.resolve_run_settings(self.store, {"provider_id": "alpha", "model": "one"})["llm"]
        self.assertEqual(config["base_url"], "https://alpha.example/v1")
        self.assertEqual(config["api_key"], SECRET)
        self.assertEqual(config["model"], "one")
        self.assertEqual(config["protocol"], "chat_completions")
        self.assertEqual(config["max_output_tokens"], 4096)
        self.assertEqual(self.store.get_meta("active_model")["provider_id"], "beta")

    def test_removed_run_provider_or_model_fails_without_falling_back_to_active(self):
        model_config.save_profile(self.store, profile())
        for run in ({"provider_id": "deleted", "model": "one"}, {"provider_id": "alpha", "model": "removed"}):
            with self.assertRaises(AgentFailure):
                model_config.resolve_run_settings(self.store, run)

    def test_discovery_reuses_key_only_for_matching_origin_and_protocol(self):
        model_config.save_profile(self.store, profile())
        requests, options = [], []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"data": [{"id": "one"}]})
        inputs = [
            {"base_url": "https://alpha.example/v1/chat/completions", "protocol": "chat_completions"},
            {"base_url": "https://beta.example/v1", "protocol": "chat_completions"},
            {"base_url": "https://alpha.example:8443/v1", "protocol": "chat_completions"},
            {"base_url": "https://alpha.example/v1", "protocol": "anthropic_messages"},
        ]
        with self.mock_network(handler, options):
            for value in inputs:
                asyncio.run(model_config.discover_models(self.store, model_config.DiscoverInput(provider_id="alpha", **value)))
        self.assertEqual(requests[0].headers["authorization"], "Bearer " + SECRET)
        self.assertEqual(requests[0].url.path, "/v1/models")
        self.assertTrue(all("authorization" not in r.headers and "x-api-key" not in r.headers for r in requests[1:]))
        self.assertTrue(all(option["follow_redirects"] is False for option in options))

    def test_anthropic_discovery_uses_required_headers_and_explicit_key(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"data": [{"id": "claude-example", "display_name": "Claude Example"}]})
        with self.mock_network(handler):
            result = asyncio.run(model_config.discover_models(self.store, model_config.DiscoverInput(base_url="https://api.example/v1/messages", protocol="anthropic_messages", api_key=SECRET)))
        self.assertEqual(requests[0].url.path, "/v1/models")
        self.assertEqual(requests[0].headers["x-api-key"], SECRET)
        self.assertEqual(requests[0].headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("authorization", requests[0].headers)
        self.assertEqual(result["models"], [{"id": "claude-example", "name": "Claude Example"}])

    def test_discovery_normalizes_models_and_ignores_invalid_entries(self):
        payload = {"models": ["local-one", {"id": "remote", "displayName": "Remote"}, {"id": "remote", "display_name": "Latest"},
                              {"name": "named-model"}, {"id": ""}, {"id": "bad\nmodel"}, 7, None]}
        with self.mock_network(lambda r: httpx.Response(200, json=payload)):
            result = asyncio.run(model_config.discover_models(self.store, model_config.DiscoverInput(base_url="http://localhost:11434/v1")))
        self.assertEqual(result["models"], [{"id": "local-one", "name": "local-one"}, {"id": "remote", "name": "Latest"}, {"id": "named-model", "name": "named-model"}])

    def test_model_list_failure_diagnostics_never_echo_upstream_secrets_or_urls(self):
        for status in (302, 400, 401, 403, 404, 429, 500):
            with self.subTest(status=status):
                body = {"error": {"type": "invalid_api_key", "message": SECRET + " https://private.example/secret"}}
                with self.mock_network(lambda r: httpx.Response(status, json=body, headers={"Location": "https://redirect.example"})), self.assertRaises(HTTPException) as caught:
                    asyncio.run(model_config.discover_models(self.store, model_config.DiscoverInput(base_url="https://alpha.example/v1", api_key=SECRET)))
                self.assertEqual(caught.exception.status_code, 502)
                self.assertNotIn(SECRET, str(caught.exception.detail))
                self.assertNotIn("private.example", str(caught.exception.detail))

    def test_model_list_invalid_json_shape_has_a_safe_manual_fallback(self):
        for payload in (None, "string", 7, True, {"data": {"not": "a-list"}}):
            with self.subTest(payload=payload), self.mock_network(lambda r: httpx.Response(200, content=json.dumps(payload))), self.assertRaises(HTTPException) as caught:
                asyncio.run(model_config.discover_models(self.store, model_config.DiscoverInput(base_url="https://alpha.example/v1")))
            self.assertEqual(caught.exception.status_code, 502)

    def test_discovery_timeout_and_size_limit_do_not_modify_saved_profiles(self):
        model_config.save_profile(self.store, profile())
        before = model_config.profiles_view(self.store)
        def timeout(request):
            raise httpx.ConnectTimeout(SECRET, request=request)
        for handler in (timeout, lambda r: httpx.Response(200, content=b"x" * 2_000_001)):
            with self.mock_network(handler), self.assertRaises(HTTPException) as caught:
                asyncio.run(model_config.discover_models(self.store, model_config.DiscoverInput(provider_id="alpha", base_url="https://alpha.example/v1")))
            self.assertNotIn(SECRET, str(caught.exception.detail))
            self.assertEqual(model_config.profiles_view(self.store), before)


if __name__ == "__main__":
    unittest.main()
