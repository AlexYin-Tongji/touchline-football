"""Choose how a source is reached: pinned local DNS, or the configured proxy.

Both routes exist for one reason: on a network whose local DNS is intercepted,
a reviewed publisher hostname resolves to a fake address, and the pinned reader
that resolves names locally can never reach it. The proxy resolves the name
instead, so only the reviewed built-in publishers may use it.

A user-configured source never resolves through the proxy unless its owner asks
for it. That is not caution for its own sake: `ingestion.public_addresses`
refuses non-global addresses by looking at the *local* resolution, and letting
such a source reach a proxy would move that resolution to a machine we do not
police — a URL that looks public here could be an internal address there.
"""
from __future__ import annotations

from urllib.parse import urlsplit
from urllib.request import getproxies

# A source may opt in explicitly; absent means "the pinned reader", which is
# what every custom source did before this module existed.
PROXY_OPT_IN_FIELD = "use_proxy"

PROXY = "proxy"
PINNED = "pinned"


def configured_proxy(settings=None, *, lookup=None):
    """The proxy the app should use, or None.

    An address saved in the app's own settings wins over the OS registry: the
    registry can carry a stale entry from a client that is no longer running,
    and on a machine whose proxy toggle is off the user still needs a way to
    say "use this one" without changing a system-wide setting.

    ``lookup`` lets a caller supply its own registry reader. Callers that read
    the OS proxy pass their module's ``getproxies`` so the lookup stays
    patchable at the point of use.
    """
    saved = _saved_proxy(settings)
    if saved:
        return saved
    read = lookup or getproxies
    values = read()
    return values.get("https") or values.get("http") or None


def _saved_proxy(settings):
    if settings is None:
        return None
    if isinstance(settings, dict):
        network = settings.get("network") or {}
    else:
        network = getattr(settings, "network", None) or {}
        if not isinstance(network, dict):
            network = network.model_dump() if hasattr(network, "model_dump") else {}
    value = (network.get("proxy_url") or "").strip()
    return value or None


def proxy_state(settings=None, *, lookup=None):
    """Whether a proxy is in play at all. Never probes reachability.

    Transport decisions read only this. Whether the address actually answers is
    a separate question answered by `network_probe.proxy_reachability`, and it
    affects the wording shown to the user and the failure backoff — not which
    reader a request uses. Keeping those apart means a proxy that is configured
    but momentarily down does not silently reroute reviewed traffic through
    local DNS, which is exactly the route that is broken on such a network.
    """
    return "configured" if configured_proxy(settings, lookup=lookup) else "absent"


def transport_for(source, *, settings=None, lookup=None):
    """``PROXY`` or ``PINNED`` for this source under the current settings."""
    if source.get("builtin"):
        return PROXY if configured_proxy(settings, lookup=lookup) else PINNED
    # An opted-in custom source still needs a proxy to exist; without one there
    # is nothing to route through and the pinned reader stays the only route.
    if source.get(PROXY_OPT_IN_FIELD) and configured_proxy(settings, lookup=lookup):
        return PROXY
    return PINNED


def proxy_host_port(settings=None):
    """``(host, port)`` for display and reachability probing, or ``(None, None)``.

    Credentials are stripped: this feeds a diagnostic that must never echo an
    authenticated proxy URL back to a client.
    """
    value = configured_proxy(settings)
    if not value:
        return None, None
    parsed = urlsplit(value if "//" in value else "//" + value)
    try:
        port = parsed.port
    except ValueError:
        return parsed.hostname, None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.hostname, port
