"""Private source credential management; operational state stays server-side."""
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from . import network_probe, network_verdict, providers, research


class CredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: SecretStr = Field(min_length=1, max_length=8192)


def register_routes(app, store):
    def keyed_source(identity):
        row = next((row for row in providers.sources(reachable=False) if row.id == identity and row.requires_key), None)
        if row is None:
            raise HTTPException(404, "没有此凭据来源。")
        return row

    def safe_source(row):
        configured = not row.requires_key or bool(store.get_secret(row.requires_key))
        result = {"id": row.id, "name": row.name, "configured": configured,
                  "requires_key": bool(row.requires_key), "capabilities": sorted(cap.value for cap in row.capabilities)}
        if row.id == "api_football":
            from .provider_quota import DailyQuota
            result["quota"] = DailyQuota(store, "api_football").status()
        return result

    @app.get("/api/data-sources")
    async def source_status():
        return {"sources": [safe_source(row) for row in providers.sources(reachable=False)]}

    @app.put("/api/data-sources/{identity}/credential")
    async def set_credential(identity: str, value: CredentialInput):
        row = keyed_source(identity)
        store.set_secret(row.requires_key, value.api_key.get_secret_value())
        return safe_source(row)

    @app.delete("/api/data-sources/{identity}/credential")
    async def clear_credential(identity: str):
        row = keyed_source(identity)
        store.set_secret(row.requires_key, None)
        return safe_source(row)

    @app.get("/api/network-diagnostics")
    async def network_diagnostics():
        """Whether the local network is the reason sources are failing.

        Reads only records the app already wrote plus two cheap local probes; it
        makes no publisher request. The response is reduced to counts and a
        verdict on purpose: proxy credentials, source URLs and response bodies
        have no place in a diagnostic the browser renders.
        """
        settings = store.transport_settings() if hasattr(store, "transport_settings") else None
        proxy = network_probe.proxy_reachability(settings)
        state = network_probe.proxy_state(settings)
        records, transport_failed, http_failed, reachable = [], 0, 0, 0
        failing_hosts = []
        for source in _diagnostic_sources(store):
            health = research.source_health(store, source)
            kind = health.get("last_error_kind")
            records.append((source, health))
            if kind in network_verdict.TRANSPORT_CODES:
                transport_failed += 1
                host = (source.get("url") or "").split("//")[-1].split("/")[0].lower()
                if host:
                    failing_hosts.append(host)
            elif kind:
                http_failed += 1
            elif health.get("last_ok_at"):
                reachable += 1
        sampled = next((host for host in failing_hosts if host), None)
        intercepted = bool(sampled) and network_probe.dns_verdict(sampled) == "intercepted"
        conclusion = network_verdict.verdict(records, state, dns_intercepted=intercepted)
        return {
            "proxy": {"state": state, "configured": proxy["configured"],
                      "host": proxy["host"], "port": proxy["port"], "listening": proxy["listening"]},
            "dns": {"intercepted": intercepted, "sample": sampled if intercepted else None},
            "sources": {"total": len(records), "transport_failed": transport_failed,
                        "http_failed": http_failed, "reachable": reachable},
            "verdict": conclusion,
            "notice": network_verdict.VERDICTS.get(conclusion) if conclusion else None,
        }


def _diagnostic_sources(store):
    """Every built-in source once, without depending on a followed team.

    Diagnosing the network must not require the reader to be following any
    particular club, and the shared publisher feeds are the ones whose failure
    carries the signal.
    """
    from .news_sources import (additional_sources, community_sources, verified_publisher_sources)
    from .club_library import TEAM_ALIASES
    seen, result = set(), []
    team_id = next(iter(sorted(TEAM_ALIASES)), None)
    candidates = [*(verified_publisher_sources()),
                  *(additional_sources(team_id) if team_id else []),
                  *(community_sources(team_id) if team_id else [])]
    for source in candidates:
        if source["id"] not in seen:
            seen.add(source["id"])
            result.append(source)
    return result
