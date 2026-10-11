"""Shared lifecycle only; each test still owns its isolated database and assertions."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from server.main import create_app


class NoSystemProxyCase(unittest.TestCase):
    """A test case that must not see the developer's real OS proxy.

    `configured_proxy` falls back to the OS registry whenever the app's own
    settings name no proxy, so any test asserting a transport decision made
    without one was really asserting it about the machine it ran on. The patch
    is applied at each module that reads the registry so it stays effective no
    matter which module the code under test calls through.
    """

    def setUp(self):
        super().setUp()
        self._proxy_patchers = []
        for module in ("server.transport_choice", "server.research", "server.research_transport"):
            patcher = patch(module + ".getproxies", return_value={})
            patcher.start()
            self._proxy_patchers.append(patcher)
        self.addCleanup(self._stop_proxy_patchers)

    def _stop_proxy_patchers(self):
        while self._proxy_patchers:
            self._proxy_patchers.pop().stop()


class IsolatedAppCase(unittest.TestCase):
    spoiler_free = None
    # Whether the app under test may see the machine's real OS proxy. Tests that
    # assert on transport must leave this off: reading the developer's own
    # registry makes the suite pass where no proxy is configured and fail where
    # one is, which is the same code behaving two ways.
    os_proxy = None

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "app.sqlite3"
        if self.os_proxy is None:
            for module in ("server.transport_choice", "server.research", "server.research_transport"):
                patcher = patch(module + ".getproxies", return_value={})
                patcher.start()
                self.addCleanup(patcher.stop)
        self.app = create_app(self.path)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.addCleanup(self.client_context.__exit__, None, None, None)
        if self.spoiler_free is not None:
            response = self.client.put("/api/settings", json={"spoiler_free": self.spoiler_free})
            self.assertEqual(response.status_code, 200, response.text)
