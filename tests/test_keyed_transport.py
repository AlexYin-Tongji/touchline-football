"""Credential headers must never leave the original HTTPS origin."""
import unittest
from unittest.mock import patch

from server import ingestion


class KeyedTransportTests(unittest.TestCase):
    def test_redirect_cannot_send_key_to_http_another_host_or_another_port(self):
        for destination in ("http://api.example/data", "https://other.example/data", "https://api.example:8443/data"):
            with self.subTest(destination=destination):
                requests = []

                class Response:
                    status = 302

                    def getheader(self, key, default=None):
                        return destination if key == "Location" else default

                class Connection:
                    def __init__(self, host, port, address, secure, timeout):
                        self.origin = (host, port, secure)

                    def request(self, method, target, headers):
                        requests.append((self.origin, headers))

                    def getresponse(self):
                        return Response()

                    def close(self):
                        pass

                with patch.object(ingestion, "public_addresses", return_value=["93.184.216.34"]), \
                     patch.object(ingestion, "_PinnedHTTP", Connection):
                    with self.assertRaises(ingestion.IngestionError):
                        ingestion.fetch_document("https://api.example/data", allowed_hosts={"api.example", "other.example"},
                                                 headers={"x-apisports-key": "private"})
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0][0], ("api.example", 443, True))

    def test_initial_plaintext_or_control_headers_are_rejected_before_connect(self):
        for url, headers in (("http://api.example/data", {"x-api-key": "private"}),
                             ("https://api.example/data", {"x-api-key": "private\rInjected: yes"}),
                             ("https://api.example/data", {"Host": "attacker.example"})):
            with self.subTest(url=url, fields=list(headers)):
                requests = []
                with patch.object(ingestion, "public_addresses", side_effect=lambda *args: requests.append(args)):
                    with self.assertRaises(ingestion.IngestionError):
                        ingestion.fetch_document(url, headers=headers)
                self.assertEqual(requests, [])
