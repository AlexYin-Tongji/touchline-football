"""A registry-backed provider: a second structured source for fixtures and tables.

ESPN is currently the only source that can answer squad, fixture or standings
questions, which makes every one of those a single point of failure and a single
opinion. This adds a second, and the interesting part is not the extra coverage
— it is what the system does when the two disagree.

Nothing is merged across providers. `provenance` keeps both claims, each with
its own tier and retrieval time, so a fixture time that one source reports and
the other does not becomes a visible difference rather than a silent overwrite.
That is the behaviour the whole claim model was built for; this is the first
provider pairing where it actually gets exercised.

Two constraints shape the implementation. The free tier is limited per minute
and per day, so the rate limit is enforced by the registry's shared pacing and
the response cache is consulted before any request — spending a metered call to
re-learn a stored value is the one mistake this provider cannot afford. And the
key is read from the named credential store, never from settings or a URL, so a
failure reports "not configured" without ever echoing it.
"""
from __future__ import annotations

import json
from urllib.parse import urlencode, urlsplit

from ..provenance import SourceTier
from .base import BaseProvider, Capability, Cost, Latency, ProviderError, provider

API_HOST = "api.football-data.org"
API_BASE = f"https://{API_HOST}/v4"
# The free tier publishes a rolling allowance; keep well inside it rather than
# discovering the ceiling by being throttled.
MIN_INTERVAL_SECONDS = 6.0
CACHE_SECONDS = 6 * 60 * 60
MAX_BODY = 2 * 1024 * 1024

# Competitions this provider is reviewed for. Restricting the list keeps a
# request from being spent on a competition nobody tracks.
COMPETITIONS = {
    "premier-league": "PL", "la-liga": "PD", "serie-a": "SA",
    "bundesliga": "BL1", "ligue-1": "FL1",
}


@provider(
    {"id": "football_data", "name": "football-data.org", "tier": SourceTier.DATASET,
     "domains": (API_HOST,), "reviewed_at": "2026-09-28"},
    {Capability.FIXTURE, Capability.STANDINGS},
    cost=Cost.KEY, latency=Latency.NORMAL, requires_key="football_data_api_key",
    note="结构化赛程与积分，免费额度受分钟与每日限制；结论与 ESPN 并列保留，不互相覆盖。",
)
class FootballDataProvider(BaseProvider):
    """Fixtures and tables from a second structured source."""

    min_interval = MIN_INTERVAL_SECONDS

    def competition(self, league_id):
        return COMPETITIONS.get(str(league_id))

    async def _get(self, path, params=None):
        """One paced, keyed request, served from cache when it is still fresh."""
        from ..ingestion import IngestionError, fetch_document, source_error_details
        from ..research_transport import shared_request
        if self.store is None:
            raise ProviderError("读取来源需要存储。", code="no_store")
        key = self.credentials()
        url = f"{API_BASE}{path}" + (("?" + urlencode(params)) if params else "")
        cache_key = "football_data_v1:" + url
        cached = self.store.get_meta(cache_key, {})
        stamp = _fresh(cached.get("fetched_at"))
        if stamp:
            return cached.get("payload"), True
        if _recent(cached.get("last_attempt")):
            # A failure inside the cooldown is not retried; that is what keeps
            # one bad minute from consuming the day's allowance.
            raise ProviderError(cached.get("last_error") or "来源暂时不可用。",
                                retryable=True, code="cached_failure")

        async def factory():
            # The key travels in a header, never in the URL: a URL is stored in
            # the shared request cache and would put the credential on disk.
            body, final_url, content_type = await fetch_document(
                url, allowed_hosts={API_HOST}, max_body=MAX_BODY,
                content_types=("application/json", "text/json", "text/plain"),
                headers={"X-Auth-Token": key})
            parts = urlsplit(final_url)
            if parts.scheme != "https" or parts.hostname != API_HOST or parts.fragment:
                raise IngestionError("来源响应地址异常，未采用。")
            try:
                payload = json.loads(body)
            except (UnicodeError, ValueError, RecursionError):
                raise IngestionError("来源返回的资料无法读取。") from None
            if not isinstance(payload, dict) or payload.get("errorCode"):
                # The upstream message is not surfaced: it can echo the request.
                raise IngestionError("来源拒绝了本次请求。")
            return payload

        await self.pacing.wait()
        try:
            payload = await shared_request(self.store, ("football_data", url), API_HOST, factory)
        except Exception as exc:
            details = source_error_details(exc)
            self.store.set_meta(cache_key, {**cached, "last_attempt": _now(),
                                            "last_error": details.get("message") or "来源暂时不可达。"})
            raise ProviderError(details.get("message") or "来源暂时不可达。",
                                retryable=True, code=details.get("code", "unreachable")) from None
        self.store.set_meta(cache_key, {"payload": payload, "fetched_at": _now()})
        return payload, False

    async def standings(self, league_id, season=None):
        code = self.competition(league_id)
        if not code:
            return self.unavailable(Capability.STANDINGS, "这个联赛尚未接入结构化赛程来源。")
        try:
            payload, cached = await self._get(f"/competitions/{code}/standings",
                                              {"season": season} if season else None)
        except ProviderError as exc:
            return self.unavailable(Capability.STANDINGS, str(exc), retryable=exc.retryable)
        rows = ((payload.get("standings") or [{}])[0] or {}).get("table") or []
        if not rows:
            return self.unavailable(Capability.STANDINGS, "来源未返回积分榜。")
        fetched = _now()
        items = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("team"):
                continue
            items.append({"provider_team": row["team"].get("id"), "name": row["team"].get("name"),
                          "rank": row.get("position"), "played": row.get("playedGames"),
                          "points": row.get("points"),
                          "claim": self._claim("standings", row.get("position"), fetched,
                                               note=f"{row['team'].get('name')} 第 {row.get('position')} 位。")})
        return self.envelope(Capability.STANDINGS, items=items, cached=cached)

    async def fixtures(self, league_id, season=None, matchday=None):
        code = self.competition(league_id)
        if not code:
            return self.unavailable(Capability.FIXTURE, "这个联赛尚未接入结构化赛程来源。")
        params = {"season": season} if season else {}
        if matchday:
            params["matchday"] = matchday
        try:
            payload, cached = await self._get(f"/competitions/{code}/matches", params)
        except ProviderError as exc:
            return self.unavailable(Capability.FIXTURE, str(exc), retryable=exc.retryable)
        matches = payload.get("matches")
        if not isinstance(matches, list) or not matches:
            return self.unavailable(Capability.FIXTURE, "来源未返回赛程。")
        fetched = _now()
        items = []
        for match in matches[:400]:
            if not isinstance(match, dict):
                continue
            home = (match.get("homeTeam") or {}).get("name")
            away = (match.get("awayTeam") or {}).get("name")
            score = ((match.get("score") or {}).get("fullTime") or {})
            items.append({
                "provider_match": match.get("id"), "kickoff": match.get("utcDate"),
                "status": match.get("status"), "competition": code,
                "home": home, "away": away, "score": _score(score),
                "matchday": match.get("matchday"),
                # Each field is claimed separately so a disagreement about the
                # kickoff does not also look like a disagreement about the score.
                "claims": {name: self._claim(name, value, fetched)
                           for name, value in (("kickoff", match.get("utcDate")),
                                               ("status", match.get("status")),
                                               ("score", _score(score))) if value not in (None, "")},
            })
        return self.envelope(Capability.FIXTURE, items=items, cached=cached)

    def _claim(self, field_name, value, fetched, *, note=""):
        from .. import provenance as prov
        source = {"id": self.source.id, "url": f"https://{API_HOST}/", "name": self.source.name}
        return prov.claim_from(source, field_name, value, fetched_at=fetched, note=note)


def _score(score):
    """The final score as one value, or nothing when a match has not finished."""
    home, away = score.get("home"), score.get("away")
    if home is None or away is None:
        return None
    return f"{home}-{away}"


def _now():
    from ..ingestion import now
    return now()


def _fresh(stamp):
    from ..ingestion import published_time
    from datetime import datetime, timezone
    value = published_time(stamp)
    if not value:
        return False
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds()
    return 0 <= age < CACHE_SECONDS


def _recent(stamp):
    from ..ingestion import published_time
    from datetime import datetime, timezone
    value = published_time(stamp)
    if not value:
        return False
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds()
    return 0 <= age < 60
