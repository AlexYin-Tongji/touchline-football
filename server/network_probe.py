"""Answer one question: is this machine's network itself the problem?

The failure mode this exists for is specific. On a network whose DNS is
intercepted, every reviewed publisher resolves to an address that is not the
publisher's, so connections time out or fail TLS. Each source reports its own
error, and the reader is shown two dozen independent-looking failures when the
truth is a single local condition.

Two signals separate those cases:

* A proxy address that is configured but nothing listening — the registry entry
  outliving the client that wrote it.
* A resolved address in a range known to be used for interception.

Neither is proof on its own, which is why `verdict` in the diagnostics route
weighs them together with how *widely* the transport is failing.
"""
from __future__ import annotations

import ipaddress

from . import transport_choice

# Ranges observed serving intercepted answers on this class of network. They are
# deliberately narrow: the point is to catch a substituted address, and a broad
# range would start flagging legitimate hosts.
KNOWN_INTERCEPTION_PREFIXES = (
    "31.13.0.0/16",        # Meta, substituted for unrelated publishers
    "199.59.148.0/24",     # Twitter, substituted for wikipedia.org
    "108.160.160.0/20",    # Dropbox, substituted for theguardian.com
    "243.185.187.0/24",    # long-standing blackhole answer
    "198.18.0.0/15",       # benchmark range some proxy clients return
    "0.0.0.0/8",
    "127.0.0.0/8",
)


def _prefixes():
    return tuple(ipaddress.ip_network(value) for value in KNOWN_INTERCEPTION_PREFIXES)


def resolve_hosts(host):
    """Every address a hostname currently resolves to, or ``[]``."""
    import socket
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    seen = []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    return seen


def address_is_intercepted(address):
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in _prefixes())


def dns_verdict(host):
    """``"intercepted"``, ``"resolved"`` or ``"unresolved"`` for one hostname.

    ``"resolved"`` only means the name produced a usable-looking public
    address; it is not a claim that the host answers.
    """
    addresses = resolve_hosts(host)
    if not addresses:
        return "unresolved"
    if any(address_is_intercepted(address) for address in addresses):
        return "intercepted"
    return "resolved"


def proxy_reachability(settings=None, *, timeout=1.5):
    """Whether something is actually listening at the configured proxy.

    Returns a dict of display-safe facts. The address itself is reduced to host
    and port: a proxy URL may carry credentials and must never be echoed.
    """
    host, port = transport_choice.proxy_host_port(settings)
    if not host:
        return {"configured": False, "host": None, "port": None, "listening": False}
    listening = _port_open(host, port, timeout)
    return {"configured": True, "host": host, "port": port, "listening": listening}


def _port_open(host, port, timeout):
    import socket
    if not port:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(timeout)
            return probe.connect_ex((host, port)) == 0
    except (OSError, ValueError):
        return False


def proxy_state(settings=None):
    """``"absent"``, ``"unusable"`` or ``"usable"`` — the diagnostic wording only.

    This never feeds the transport decision. A proxy that is configured but
    down must not make the reader fall back to local DNS, which is exactly the
    broken path on the network this is diagnosing.
    """
    if transport_choice.proxy_state(settings) == "absent":
        return "absent"
    return "usable" if proxy_reachability(settings)["listening"] else "unusable"
