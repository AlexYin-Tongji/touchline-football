"""Shared lifecycle only; each test still owns its isolated database and assertions."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from server.main import create_app


class IsolatedAppCase(unittest.TestCase):
    spoiler_free = None

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "app.sqlite3"
        self.app = create_app(self.path)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.addCleanup(self.client_context.__exit__, None, None, None)
        if self.spoiler_free is not None:
            response = self.client.put("/api/settings", json={"spoiler_free": self.spoiler_free})
            self.assertEqual(response.status_code, 200, response.text)
