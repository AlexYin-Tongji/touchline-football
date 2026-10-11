"""Per-host transport routing: what may change a route, and what may not.

The behaviour these pin down: reachability is recorded per host because it is a
property of the host on this network; a publisher's own answer (403, 405, 429)
never changes a route; and a source that was only ever allowed the pinned
reader can never end up on the proxy through this table.
"""
import unittest

from server import host_routes, transport_choice


SAVED = {"network": {"proxy_url": "http://127.0.0.1:7897"}}
NONE_SAVED = {"network": {"proxy_url": ""}}
# A proxy lookup that answers "none", so an empty saved address really means no
# proxy instead of falling through to whatever the machine running the suite has
# configured.
NO_PROXY = lambda: {}


class FakeStore:
    """Minimal meta store; the routing table is just a document."""

    def __init__(self):
        self.meta = {}

    def get_meta(self, key, default=None):
        return self.meta.get(key, default)

    def set_meta(self, key, value):
        self.meta[key] = value


class RouteTableTests(unittest.TestCase):
    def test_a_host_already_seeded_to_the_proxy_starts_there(self):
        # These are the hosts measured as unreachable on local DNS. Starting
        # from the seed saves a timeout on the first cycle after a restart.
        source = {"id": "x", "url": "https://www.bbc.com/sport", "builtin": True}
        self.assertEqual(host_routes.route_for(source, store=FakeStore(), settings=SAVED), host_routes.PROXY)

    def test_a_host_measured_as_local_only_is_not_sent_through_the_proxy(self):
        source = {"id": "x", "url": "https://www.hupu.com/", "builtin": True}
        self.assertEqual(host_routes.route_for(source, store=FakeStore(), settings=SAVED), host_routes.PINNED)

    def test_a_measurement_wins_over_the_seed_for_that_host(self):
        store = FakeStore()
        # bbc.com is seeded to the proxy; a real measurement says otherwise.
        host_routes.record_route(store, "www.bbc.com", host_routes.PINNED)
        source = {"id": "x", "url": "https://www.bbc.com/sport", "builtin": True}
        self.assertEqual(host_routes.route_for(source, store=store, settings=SAVED), host_routes.PINNED)

    def test_a_publisher_status_code_records_a_working_route(self):
        # A 403 or a 405 means the host answered. Recording it as a failure
        # would move traffic to the other reader for a permission error, which
        # is a different problem with a different fix.
        store = FakeStore()
        for kind in ("http_forbidden", "http_error", "http_rate_limited", "access_challenge", "content_unavailable"):
            with self.subTest(kind=kind):
                host_routes.record_route(store, "example.test", host_routes.PINNED, error_kind=kind)
                self.assertEqual(host_routes.recorded_route(store, "example.test"), host_routes.PINNED)

    def test_a_transport_failure_marks_the_route_unreachable(self):
        store = FakeStore()
        for kind in sorted(host_routes.TRANSPORT_FAILURES):
            with self.subTest(kind=kind):
                host_routes.record_route(store, "example.test", host_routes.PINNED, error_kind=kind)
                record = store.get_meta(host_routes.ROUTE_KEY)["example.test"]
                self.assertFalse(record["reachable"])

    def test_a_user_source_can_never_be_moved_to_the_proxy_by_the_table(self):
        # The pinned reader resolves locally, and that resolution is where the
        # check against internal addresses happens. A learned route must not be
        # able to move a user's source off it.
        store = FakeStore()
        host_routes.record_route(store, "user.example", host_routes.PROXY)
        source = {"id": "x", "url": "https://user.example/feed", "builtin": False}
        self.assertEqual(host_routes.route_for(source, store=store, settings=SAVED), host_routes.PINNED)

    def test_an_opted_in_user_source_may_take_a_recorded_route(self):
        store = FakeStore()
        host_routes.record_route(store, "user.example", host_routes.PROXY)
        source = {"id": "x", "url": "https://user.example/feed", "builtin": False, "use_proxy": True}
        self.assertEqual(host_routes.route_for(source, store=store, settings=SAVED), host_routes.PROXY)

    def test_without_a_proxy_the_only_route_is_the_pinned_reader(self):
        source = {"id": "x", "url": "https://www.bbc.com/sport", "builtin": True}
        self.assertEqual(host_routes.route_for(source, store=FakeStore(), settings=NONE_SAVED, lookup=NO_PROXY), host_routes.PINNED)

    def test_a_stale_failure_expires_but_a_stale_success_lingers(self):
        store = FakeStore()
        host_routes.record_route(store, "a.test", host_routes.PINNED)
        host_routes.record_route(store, "b.test", host_routes.PINNED, error_kind="timeout")
        table = store.get_meta(host_routes.ROUTE_KEY)
        # Both are fresh now.
        self.assertIsNotNone(host_routes.recorded_route(store, "a.test"))
        self.assertIsNotNone(host_routes.recorded_route(store, "b.test"))
        # Age the failure past its short window; the success outlives it.
        table["b.test"]["measured_at"] -= host_routes.BAD_SECONDS + 1
        table["a.test"]["measured_at"] -= host_routes.BAD_SECONDS + 1
        self.assertIsNotNone(host_routes.recorded_route(store, "a.test"))
        self.assertIsNone(host_routes.recorded_route(store, "b.test"))

    def test_the_table_stays_bounded(self):
        store = FakeStore()
        for index in range(300):
            host_routes.record_route(store, f"host{index}.test", host_routes.PINNED)
        self.assertLessEqual(len(store.get_meta(host_routes.ROUTE_KEY)), 256)

    def test_a_missing_or_broken_table_never_raises(self):
        class Broken:
            def get_meta(self, key, default=None):
                raise RuntimeError("no meta")

            def set_meta(self, key, value):
                raise RuntimeError("no meta")

        self.assertIsNone(host_routes.recorded_route(Broken(), "x.test"))
        host_routes.record_route(Broken(), "x.test", host_routes.PINNED)  # must not raise

    def test_host_of_ignores_ports_credentials_and_case(self):
        self.assertEqual(host_routes.host_of("https://User:pass@WWW.BBC.COM:443/x"), "www.bbc.com")
        self.assertEqual(host_routes.host_of("not a url"), "")


if __name__ == "__main__":
    unittest.main()
