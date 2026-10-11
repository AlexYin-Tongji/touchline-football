"""Which reader reaches which host, recorded per host instead of per app.

`transport_choice` answers a coarse question: does a proxy exist, and is this a
reviewed publisher. That is the right answer for a source whose owner has not
said anything else, but it cannot express what was measured on this class of
network — that reachability is a property of the *host*, and the property runs
both ways:

* `www.bbc.com`, `en.wikipedia.org` and the rest of the Wikimedia family resolve
  to substituted addresses here, so local DNS can never reach them, while the
  proxy can.
* `www.hupu.com` and `www.juventus.com` answer to local DNS and fail through the
  proxy.

A single global switch therefore has to be wrong for one group or the other.
This module keeps a small per-host table so each host takes the route that
actually reaches it.

Two rules bound what the table may do, and both are load-bearing:

* **Only a transport failure may change a route.** A 403, 405 or 429 is the
  publisher answering, which proves the route works. Rerouting on a status code
  would be treating a coincidence as a cause, and would let an ordinary
  permission error silently move traffic between readers.
* **Learned routes are only ever chosen between routes that are already
  allowed.** A custom source has one allowed route (pinned) unless its owner
  opted in, so it can never be routed through the proxy by this module. The
  proxy list inside `research_transport` is the same fixed set it always was.
"""
from __future__ import annotations

import time
from urllib.parse import urlsplit

from . import transport_choice

PINNED = transport_choice.PINNED
PROXY = transport_choice.PROXY
ABSENT = "absent"

ROUTE_KEY = "host_routes_v1"

# Failures that say "the host was never reached". An HTTP status is deliberately
# absent from this set: the far end answered, so the route is fine.
TRANSPORT_FAILURES = frozenset({
    "dns_failed", "timeout", "connection_error", "connection_reset", "tls_error", "tls_certificate",
})

# How long a measurement stays trustworthy. A reachable host stays reachable for
# a long time; a failure is often the proxy being restarted, so it is retried
# soon rather than pinned for a day.
GOOD_SECONDS = 12 * 3600
BAD_SECONDS = 300

# Reviewed publishers measured as reachable only through the proxy on a network
# whose DNS substitutes their addresses. These are the hosts the screenshot
# "off: some domains fail" / "on: other domains fail" complaint was about.
# Seeding stops the first cycle after a restart from wasting a timeout each.
BLOCKED_LOCALLY_HOSTS = frozenset({
    "www.bbc.com", "feeds.bbci.co.uk", "www.theguardian.com", "www.reddit.com",
    "en.wikipedia.org", "commons.wikimedia.org", "upload.wikimedia.org",
    "www.wikidata.org", "thumb.wikimedia.org",
    "www.fcbarcelona.com", "www.asm-fc.com",
})

# Hosts measured as answerable to local DNS and refused by the proxy. Routing
# these through the proxy is worse than leaving them alone.
PINNED_HOSTS = frozenset({
    "www.hupu.com", "www.juventus.com",
})


def host_of(url):
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def _entry(table, host):
    record = table.get(host)
    return record if isinstance(record, dict) else None


def _fresh(record, now):
    measured = record.get("measured_at")
    if not isinstance(measured, (int, float)):
        return False
    window = GOOD_SECONDS if record.get("reachable") else BAD_SECONDS
    return now - measured < window


def recorded_route(store, host):
    """The measured route for a host, or ``None`` when there is no fresh one."""
    if store is None or not host or not hasattr(store, "get_meta"):
        return None
    try:
        table = store.get_meta(ROUTE_KEY, {})
    except Exception:
        # A cache that cannot be read is an empty cache, never a failed read.
        return None
    record = _entry(table, host)
    if not record or not _fresh(record, time.time()):
        return None
    route = record.get("route")
    return route if route in {PINNED, PROXY, ABSENT} else None


def seed_route(host):
    """The route this class of network is known to need, before measuring.

    This is a starting point, not a policy: the first successful or failed
    attempt overwrites it with what this machine actually observed.
    """
    if host in BLOCKED_LOCALLY_HOSTS:
        return PROXY
    if host in PINNED_HOSTS:
        return PINNED
    return None


def allowed_routes(source, *, settings=None, lookup=None):
    """Every route this source may take, in preference order.

    A reviewed publisher may use either reader. A user-configured source has
    exactly one — the pinned reader — unless its owner opted in, because the
    address check that keeps it out of internal space runs on the *local*
    resolver and moving it to the proxy would move that check somewhere we do
    not police.
    """
    proxy = transport_choice.configured_proxy(settings, lookup=lookup)
    if source.get("builtin"):
        return [PINNED, PROXY] if proxy else [PINNED]
    if source.get(transport_choice.PROXY_OPT_IN_FIELD) and proxy:
        return [PINNED, PROXY]
    return [PINNED]


def route_for(source, *, store=None, settings=None, lookup=None):
    """The route to use for this source's host right now.

    Order of authority: a fresh measurement, then the seed for this network,
    then the pinned reader. Every candidate is filtered through
    ``allowed_routes``, so a route the source is not allowed to take is never
    returned even if the table holds it.

    The fallback is PINNED rather than the site-wide default, and that is a
    measured choice. Both readers reach most hosts on this network, but the
    proxy costs four to six seconds where local DNS costs one — measured against
    Sky (6.0s vs 1.3s) and ESPN (5.7s vs 1.0s). Since the pinned reader is the
    faster route everywhere it works, it is the right place to start, and the
    proxy is reserved for the hosts that need it. Starting on the proxy would
    make every host pay the slower route until something happened to correct it.
    """
    allowed = allowed_routes(source, settings=settings, lookup=lookup)
    if len(allowed) == 1:
        return allowed[0]
    host = host_of(source.get("url")) or host_of((source.get("_hosts") or [""])[0])
    for candidate in (recorded_route(store, host), seed_route(host)):
        if candidate in allowed:
            return candidate
    return allowed[0]

def record_route(store, host, route, *, error_kind=None):
    """Remember which route reached a host, or that none did.

    ``error_kind`` is the classification from ``ingestion.source_error_details``.
    Only transport failures are recorded as a failure; a status code from the
    publisher records a *success*, because the request did reach the host.
    """
    if store is None or not host or not hasattr(store, "set_meta"):
        return
    if route not in {PINNED, PROXY}:
        return
    reachable = error_kind is None or error_kind not in TRANSPORT_FAILURES
    try:
        with store.atomic() if hasattr(store, "atomic") else _null():
            table = store.get_meta(ROUTE_KEY, {})
            if not isinstance(table, dict):
                table = {}
            table[host] = {"route": route, "reachable": bool(reachable),
                           "measured_at": time.time(), "error_kind": error_kind or None}
            # The table is a cache, not a log; keep it bounded.
            if len(table) > 256:
                for stale in sorted(table, key=lambda key: table[key].get("measured_at") or 0)[:64]:
                    table.pop(stale, None)
            store.set_meta(ROUTE_KEY, table)
    except Exception:
        # A routing hint must never be the reason a read fails.
        return


def record_failure(store, host, error_kind):
    """Note that the host could not be reached by any route tried."""
    if store is None or not host or not hasattr(store, "set_meta"):
        return
    if error_kind not in TRANSPORT_FAILURES:
        return
    try:
        with store.atomic() if hasattr(store, "atomic") else _null():
            table = store.get_meta(ROUTE_KEY, {})
            if not isinstance(table, dict):
                table = {}
            table[host] = {"route": ABSENT, "reachable": False, "measured_at": time.time(),
                           "error_kind": error_kind}
            store.set_meta(ROUTE_KEY, table)
    except Exception:
        return


def _null():
    from contextlib import nullcontext
    return nullcontext()
