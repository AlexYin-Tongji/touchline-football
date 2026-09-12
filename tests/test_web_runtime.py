"""Deployment boundaries exercised through real HTTP middleware and cookies."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.main import create_app
from server.web_runtime import COOKIE, WebRuntime


class HostedAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "TOUCHLINE_SERVER_MODE": "true", "TOUCHLINE_PUBLIC_URL": "https://touchline.example",
            "TOUCHLINE_ACCESS_PASSWORD": "private-fixture-password-only", "RENDER": "", "RENDER_EXTERNAL_HOSTNAME": "",
        })
        self.env.start()
        self.app = create_app(Path(self.tmp.name) / "app.db")
        self.client = TestClient(self.app, base_url="https://touchline.example")
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.tmp.cleanup()

    def login(self):
        return self.client.post("/api/auth/login", json={"password": "private-fixture-password-only"}, headers={"Origin": "https://touchline.example"})

    def test_private_data_requires_session_and_logout_revokes_it(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/api/state").status_code, 401)
        self.assertFalse(self.client.get("/api/auth/status").json()["authenticated"])
        result = self.login()
        self.assertEqual(result.status_code, 200)
        for flag in ("HttpOnly", "Secure", "SameSite=strict"):
            self.assertIn(flag, result.headers["set-cookie"])
        cookie = self.client.cookies.get(COOKIE)
        self.assertEqual(self.client.get("/api/state").json()["runtime"], {"server_mode": True, "storage": "server"})
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/state", headers={"Cookie": COOKIE + "=" + cookie}).status_code, 401)

    def test_origin_port_and_host_checked_even_with_a_valid_session(self):
        self.login()
        for origin in ("https://attacker.example", "https://touchline.example:444", "http://touchline.example", "null"):
            self.assertEqual(self.client.put("/api/settings", json={"mode": "live"}, headers={"Origin": origin}).status_code, 403)
        self.assertEqual(self.client.put("/api/settings", json={"mode": "live"}, headers={"Origin": "https://touchline.example"}).status_code, 200)
        self.assertEqual(self.client.get("/api/health", headers={"Host": "attacker.example"}).status_code, 400)
        self.assertEqual(self.client.post("/api/auth/login", json={"password": "private-fixture-password-only"}, headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)

    def test_login_rate_limit_and_expired_session(self):
        for _ in range(8):
            self.assertEqual(self.client.post("/api/auth/login", json={"password": "wrong"}).status_code, 401)
        limited = self.login()
        self.assertEqual(limited.status_code, 429)
        self.assertIn("retry-after", limited.headers)
        self.app.state.access.attempts.clear()
        self.login()
        for token in self.app.state.access.sessions:
            self.app.state.access.sessions[token] = 1
        self.assertEqual(self.client.get("/api/state").status_code, 401)

    def test_hosted_mode_blocks_local_machine_controls_and_body_limit(self):
        self.login()
        self.assertEqual(self.client.post("/api/service/shortcut").status_code, 409)
        self.assertEqual(self.client.put("/api/service/autostart", json={"enabled": True}).status_code, 409)
        self.assertEqual(self.client.post("/api/import", content="x" * (256 * 1024 + 1)).status_code, 413)
        self.assertEqual(self.client.get("/api/service").json()["url"], "https://touchline.example")
        self.assertEqual(self.client.get("/api/state").headers["cache-control"], "no-store")

    def test_personalization_merges_persists_and_validates(self):
        self.login()
        response = self.client.put("/api/settings", json={"personalization": {"interests": ["战术", "青训", "战术"], "fan_level": "expert"}})
        self.assertEqual(response.status_code, 200)
        saved = self.client.put("/api/settings", json={"personalization": {"reading_language": "original"}}).json()["personalization"]
        self.assertEqual(saved["interests"], ["战术", "青训"])
        self.assertEqual(saved["fan_level"], "expert")
        self.assertEqual(saved["briefing_window_hours"], 72)
        for values in ({"briefing_window_hours": 0}, {"interests": [""]}, {"fan_level": "invalid"}):
            self.assertEqual(self.client.put("/api/settings", json={"personalization": values}).status_code, 422)
        self.assertNotIn("private-fixture-password-only", self.client.get("/api/state").text)


class RuntimeConfigTests(unittest.TestCase):
    def test_fail_closed_without_strong_password_or_public_https_origin(self):
        base = {"TOUCHLINE_SERVER_MODE": "true", "TOUCHLINE_ACCESS_PASSWORD": "", "TOUCHLINE_PUBLIC_URL": "", "RENDER_EXTERNAL_HOSTNAME": ""}
        with patch.dict(os.environ, base):
            with self.assertRaises(RuntimeError):
                WebRuntime.from_env()
            os.environ["TOUCHLINE_ACCESS_PASSWORD"] = "private-fixture-password-only"
            with self.assertRaises(RuntimeError):
                WebRuntime.from_env()
            os.environ["TOUCHLINE_PUBLIC_URL"] = "http://public.example"
            with self.assertRaises(RuntimeError):
                WebRuntime.from_env()
            os.environ["TOUCHLINE_PUBLIC_URL"] = "http://localhost:8765"
            self.assertFalse(WebRuntime.from_env().secure_cookie)

    def test_render_hostname_is_discovered(self):
        with patch.dict(os.environ, {"RENDER": "true", "TOUCHLINE_SERVER_MODE": "", "TOUCHLINE_PUBLIC_URL": "", "RENDER_EXTERNAL_HOSTNAME": "touchline.onrender.com", "TOUCHLINE_ACCESS_PASSWORD": "private-fixture-password-only"}):
            runtime = WebRuntime.from_env()
            self.assertTrue(runtime.server_mode)
            self.assertEqual(runtime.public_url, "https://touchline.onrender.com")


if __name__ == "__main__":
    unittest.main()
