"""Transport choice and network diagnosis.

The behaviour these pin down: which reader a source uses is decided by whether
a proxy exists, never by whether the proxy currently answers; and a wave of
transport failures across unrelated publishers is reported as one local
condition rather than as many broken sources.
"""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server import network_probe, network_verdict, research, source_config, transport_choice
from tests.support import IsolatedAppCase, NoSystemProxyCase


SAVED = {"network": {"proxy_url": "http://127.0.0.1:7897"}}
NONE_SAVED = {"network": {"proxy_url": ""}}


class TransportChoiceTests(NoSystemProxyCase):
    def test_a_reviewed_publisher_uses_the_proxy_only_when_one_exists(self):
        self.assertEqual(transport_choice.transport_for({"builtin": True}, settings=SAVED), transport_choice.PROXY)
        self.assertEqual(transport_choice.transport_for({"builtin": True}, settings=NONE_SAVED), transport_choice.PINNED)

    def test_a_user_source_keeps_the_pinned_reader_unless_it_opts_in(self):
        # Letting it through by default would move the address check from the
        # local resolver to the proxy, where a URL that looks public here can
        # point at an internal address.
        self.assertEqual(transport_choice.transport_for({"builtin": False}, settings=SAVED), transport_choice.PINNED)
        self.assertEqual(
            transport_choice.transport_for({"builtin": False, "use_proxy": True}, settings=SAVED),
            transport_choice.PROXY)
        # Opting in without a proxy to route through changes nothing.
        self.assertEqual(
            transport_choice.transport_for({"builtin": False, "use_proxy": True}, settings=NONE_SAVED),
            transport_choice.PINNED)

    def test_a_saved_address_wins_over_the_os_registry(self):
        with patch("server.transport_choice.getproxies", return_value={"https": "http://os-proxy.invalid:9999"}):
            self.assertEqual(transport_choice.configured_proxy(SAVED), "http://127.0.0.1:7897")
            self.assertEqual(transport_choice.configured_proxy(NONE_SAVED), "http://os-proxy.invalid:9999")

    def test_the_os_registry_is_read_through_the_caller_for_patchability(self):
        with patch.object(research, "getproxies", return_value={"https": "http://proxy.invalid:8080"}):
            self.assertEqual(transport_choice.configured_proxy(lookup=research.getproxies), "http://proxy.invalid:8080")

    def test_a_proxy_address_never_exposes_its_credentials(self):
        host, port = transport_choice.proxy_host_port({"network": {"proxy_url": "http://user:secret@10.0.0.1:7897"}})
        self.assertEqual((host, port), ("10.0.0.1", 7897))

    def test_state_reports_only_whether_an_address_exists(self):
        # Reachability is deliberately not part of this: a proxy that is
        # configured but down must not reroute reviewed traffic to local DNS.
        self.assertEqual(transport_choice.proxy_state(SAVED), "configured")
        self.assertEqual(transport_choice.proxy_state(NONE_SAVED), "absent")


class InterceptionDetectionTests(NoSystemProxyCase):
    def test_addresses_observed_serving_intercepted_answers_are_flagged(self):
        for address in ("31.13.67.41", "199.59.148.96", "108.160.165.11", "0.0.0.0"):
            self.assertTrue(network_probe.address_is_intercepted(address), address)

    def test_ordinary_public_addresses_are_not_flagged(self):
        for address in ("18.65.14.126", "185.250.250.33", "103.102.166.240", "104.17.208.5", "20.205.243.166"):
            self.assertFalse(network_probe.address_is_intercepted(address), address)

    def test_proxy_state_separates_absent_from_unreachable(self):
        self.assertEqual(network_probe.proxy_state(NONE_SAVED), "absent")
        # The probe must be able to tell a listening port from a closed one.
        # Port 9 is the discard service and is never bound here; naming a port
        # that this machine happens to have a proxy on would make the assertion
        # a statement about the developer's desktop instead of about the probe.
        with patch.object(network_probe, "_port_open", return_value=False):
            self.assertEqual(network_probe.proxy_state(SAVED), "unusable")
        with patch.object(network_probe, "_port_open", return_value=True):
            self.assertEqual(network_probe.proxy_state(SAVED), "usable")


class VerdictTests(unittest.TestCase):
    @staticmethod
    def _record(source_id, host, kind):
        return ({"id": source_id, "url": "https://" + host + "/feed"}, {"last_error_kind": kind})

    def test_failures_across_unrelated_publishers_are_reported_once(self):
        records = [self._record("bbc", "feeds.bbci.co.uk", "dns_failed"),
                   self._record("guardian", "www.theguardian.com", "timeout"),
                   self._record("espn", "www.espn.com", "connection_reset")]
        self.assertEqual(network_verdict.verdict(records, "absent"), "local_network_blocked")

    def test_one_publisher_failing_is_that_publisher_problem(self):
        records = [self._record("bbc", "feeds.bbci.co.uk", "dns_failed"),
                   self._record("sky", "feeds.bbci.co.uk", "dns_failed")]
        self.assertIsNone(network_verdict.verdict(records, "absent"))

    def test_a_publisher_answering_is_never_a_local_network_verdict(self):
        # 403 and 404 mean the route works; only reaching the host counts.
        records = [self._record("a", "one.example", "http_forbidden"),
                   self._record("b", "two.example", "http_unavailable"),
                   self._record("c", "three.example", "content_unavailable")]
        self.assertIsNone(network_verdict.verdict(records, "absent"))
        self.assertEqual(network_verdict.failing_domains(records), set())

    def test_a_configured_but_dead_proxy_is_named(self):
        records = [self._record("bbc", "feeds.bbci.co.uk", "timeout")]
        self.assertEqual(network_verdict.verdict(records, "unusable"), "proxy_unusable")

    def test_a_working_proxy_suppresses_the_verdict(self):
        records = [self._record("bbc", "feeds.bbci.co.uk", "dns_failed"),
                   self._record("guardian", "www.theguardian.com", "timeout"),
                   self._record("espn", "www.espn.com", "connection_reset")]
        self.assertIsNone(network_verdict.verdict(records, "usable"))


class DiagnosticsRouteTests(IsolatedAppCase):
    def test_the_route_answers_without_reaching_any_publisher(self):
        with patch.object(research.ingestion, "fetch_document", side_effect=AssertionError("no network")):
            response = self.client.get("/api/network-diagnostics")
        self.assertEqual(response.status_code, 200, response.text)

    def test_the_response_carries_no_credentials_urls_or_bodies(self):
        with patch.object(network_probe, "proxy_reachability",
                          return_value={"configured": True, "host": "127.0.0.1", "port": 7897, "listening": False}):
            payload = self.client.get("/api/network-diagnostics").json()
        self.assertEqual(set(payload), {"proxy", "dns", "sources", "verdict", "notice"})
        self.assertEqual(set(payload["proxy"]), {"state", "configured", "host", "port", "listening"})
        self.assertEqual(set(payload["dns"]), {"intercepted", "sample"})
        self.assertEqual(set(payload["sources"]), {"total", "transport_failed", "http_failed", "reachable"})

    def test_a_saved_proxy_shows_as_configured_without_echoing_its_address(self):
        self.client.put("/api/settings", json={"network": {"proxy_url": "http://user:secret@127.0.0.1:7897"}})
        with patch.object(network_probe, "_port_open", return_value=False):
            payload = self.client.get("/api/network-diagnostics").json()
        self.assertTrue(payload["proxy"]["configured"])
        self.assertEqual(payload["proxy"]["host"], "127.0.0.1")
        # The authenticated form of the address must not travel to the browser.
        rendered = str(payload)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("user:", rendered)
        self.assertEqual(payload["proxy"]["state"], "unusable")

    def test_a_failure_wave_produces_one_verdict_instead_of_many_gaps(self):
        store = self.app.state.store
        sources = [source for source in source_config._diagnostic_sources(store)][:3]
        self.assertGreaterEqual(len(sources), 3)
        for source in sources:
            store.set_meta(research._source_health_key(source),
                           {"last_error_kind": "dns_failed", "last_error_message": "域名无法解析",
                            "consecutive_failures": 1, "backoff_until": research.ingestion.now()})
        with patch.object(network_probe, "dns_verdict", return_value="intercepted"):
            payload = self.client.get("/api/network-diagnostics").json()
        self.assertEqual(payload["sources"]["transport_failed"], 3)
        self.assertEqual(payload["verdict"], "local_network_blocked")
        self.assertIn("代理", payload["notice"])

    def test_failures_from_distinct_publishers_are_required_for_the_verdict(self):
        # One publisher failing twice is that publisher's problem, not the
        # local network's, so the reader keeps the ordinary per-source notice.
        store = self.app.state.store
        sources = source_config._diagnostic_sources(store)
        same_host = [source for source in sources
                     if source.get("allowed_hosts") and len(set(source["allowed_hosts"])) == 1][:2]
        self.assertGreaterEqual(len(same_host), 2)
        for source in same_host:
            store.set_meta(research._source_health_key(source),
                           {"last_error_kind": "dns_failed", "last_error_message": "域名无法解析",
                            "consecutive_failures": 1, "backoff_until": research.ingestion.now()})
        with patch.object(network_probe, "dns_verdict", return_value="resolved"):
            payload = self.client.get("/api/network-diagnostics").json()
        self.assertIsNone(payload["verdict"])
        self.assertIsNone(payload["notice"])
