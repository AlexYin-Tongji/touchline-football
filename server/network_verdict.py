"""Explain a wave of source failures as one local-network condition.

A reader on an ordinary network sees "域名暂时无法解析" against every publisher
in the list. That reads as two dozen broken sources. When the transport is
failing the same way across unrelated domains, it is one condition, and saying
so once is both truer and more actionable than saying it twenty times.

The distinction this draws:

* A transport failure against a *single* publisher is that publisher's problem.
* The same transport failure across three or more unrelated publishers is the
  route to the network, not the far end.

Only codes that describe reaching the host count. An HTTP 403 or a 404 is the
publisher answering, which means the route works.
"""
from __future__ import annotations

from urllib.parse import urlsplit

# Codes that mean "we never reached the host", as opposed to "the host
# answered and we did not like the answer".
TRANSPORT_CODES = frozenset({
    "dns_failed", "timeout", "connection_error", "connection_reset", "tls_error", "tls_certificate",
})
# Below this many independent domains, a shared cause is not established.
MIN_FAILING_DOMAINS = 3

VERDICTS = {
    "local_network_blocked": "本机网络暂时无法直连这些国外来源，已跳过本轮重试；配置代理后会自动恢复。",
    "proxy_unusable": "检测到代理地址但端口未响应，国外来源本轮无法访问。",
    "proxy_missing": "未检测到可用代理，国外来源在本机网络下无法访问。",
}


def _domain(source, entry):
    host = (entry.get("host") if isinstance(entry, dict) else None) or urlsplit(
        (source.get("url") if isinstance(source, dict) else "") or "").hostname or ""
    return host.lower()


def failing_domains(health_records):
    """Distinct domains whose most recent failure was a transport failure.

    ``health_records`` is an iterable of ``(source, health)`` pairs, where
    ``health`` carries ``last_error_kind``. Sources that failed for a
    publisher-side reason are excluded: those do not point at the local route.
    """
    domains = set()
    for source, health in health_records:
        if not isinstance(health, dict):
            continue
        kind = health.get("last_error_kind") or ""
        if kind not in TRANSPORT_CODES:
            continue
        domain = _domain(source, health)
        if domain:
            domains.add(domain)
    return domains


def verdict(health_records, proxy_state, *, dns_intercepted=False):
    """The single conclusion to show, or ``None`` if sources should stand alone.

    ``proxy_state`` is ``network_probe.proxy_state`` output. ``dns_intercepted``
    reports whether a sampled publisher hostname resolves into a known
    interception range.
    """
    domains = failing_domains(health_records)
    if proxy_state == "absent":
        # Without an address to route through, a wide transport failure is the
        # expected shape on an intercepted network. Say so once.
        if len(domains) >= MIN_FAILING_DOMAINS or (dns_intercepted and domains):
            return "local_network_blocked"
        return None
    if proxy_state == "unusable":
        # A configured-but-dead proxy explains the wave on its own, so it is
        # reported even before the failure count crosses the threshold.
        return "proxy_unusable" if domains or dns_intercepted else None
    return None


def suppressed(sources_payload):
    """Whether per-source gap notices should be hidden behind one verdict.

    Called by the frontend via the payload it already receives; kept here so the
    threshold lives in one place.
    """
    return bool(sources_payload.get("verdict"))
