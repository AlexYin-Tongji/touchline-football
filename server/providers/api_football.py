"""API-Football v3, using only explicit API-Football identifiers.

Internal club/league slugs and ESPN IDs are deliberately not interpreted as
API-Football IDs. Callers must supply the ID obtained from this provider.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from .. import provenance
from ..provider_quota import DailyQuota, WINDOW
from ..provenance import SourceTier
from .base import BaseProvider, Capability, Cost, Latency, ProviderError, provider


API_HOST = "v3.football.api-sports.io"
API_BASE = f"https://{API_HOST}"
SECRET_NAME = "api_football_api_key"
CACHE_SECONDS = {"players/squads": 12 * 3600, "fixtures": 3600, "standings": 3600,
                 "leagues": 24 * 3600, "teams": 24 * 3600}
MAX_BODY = 2 * 1024 * 1024
MIN_INTERVAL_SECONDS = 6.0  # ten requests per minute on the free tier
_PACE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0
LEAGUE_IDENTITIES = {
    # The local names/countries come from the reviewed league catalog. API
    # names are matched exactly, never used as guessed numeric IDs.
    "PL": ("Premier League", "英格兰", "England", {"Premier League"}),
    "PD": ("LaLiga", "西班牙", "Spain", {"LaLiga", "La Liga"}),
    "SA": ("Serie A", "意大利", "Italy", {"Serie A"}),
    "BL1": ("Bundesliga", "德国", "Germany", {"Bundesliga"}),
    "FL1": ("Ligue 1", "法国", "France", {"Ligue 1"}),
}


class ApiFootballFailure(ProviderError):
    def __init__(self, message, *, code, spent=False, retryable=False, next_available_at=""):
        super().__init__(message, code=code, retryable=retryable)
        self.spent = spent
        self.next_available_at = next_available_at


@provider(
    {"id": "api_football", "name": "API-Football", "tier": SourceTier.DATASET,
     "domains": (API_HOST,), "reviewed_at": "2026-09-28"},
    {Capability.SQUAD, Capability.FIXTURE, Capability.STANDINGS},
    cost=Cost.KEY, latency=Latency.NORMAL, requires_key=SECRET_NAME,
    note="v3 结构化阵容、赛程和积分；只接受 API-Football 自身的球队/联赛 ID，滚动 24 小时限额。",
)
class ApiFootballProvider(BaseProvider):
    min_interval = MIN_INTERVAL_SECONDS

    def _gap(self, capability, error: ApiFootballFailure):
        gap = {"source_id": self.source.id, "code": error.code, "message": str(error),
               "retryable": error.retryable}
        if error.next_available_at:
            gap["next_available_at"] = error.next_available_at
        return self.envelope(capability, gaps=[gap],
                             spent=Cost.QUOTA if error.spent else Cost.FREE)

    async def resolve_team_scope(self, team_id, year=None):
        """Discover and persist native IDs for one reviewed local season team."""
        from ..team_center import team_season_context

        if self.store is None:
            return self._gap(Capability.SQUAD, ApiFootballFailure(
                "读取球队映射需要持久存储。", code="no_store"))
        try:
            self.credentials()
        except ProviderError:
            return self._gap(Capability.SQUAD, ApiFootballFailure(
                "API-Football 尚未配置密钥。", code="missing_credential"))
        try:
            team, league, season, _ = team_season_context(self.store, team_id, year)
        except Exception:
            return self._gap(Capability.SQUAD, ApiFootballFailure(
                "本地球队或完整赛季目录尚未确认。", code="mapping_required"))
        identity = LEAGUE_IDENTITIES.get(league.get("id"))
        if not identity or (league.get("english_name"), league.get("country")) != identity[:2]:
            return self._gap(Capability.SQUAD, ApiFootballFailure(
                "联赛身份不在受审范围。", code="mapping_required"))
        local_name = team.get("english_name")
        names = {_exact_name(value) for value in [local_name, *(team.get("aliases") or [])]
                 if _english_alias(value)}
        names.discard("")
        if not local_name or _exact_name(local_name) not in names:
            return self._gap(Capability.SQUAD, ApiFootballFailure(
                "球队缺少明确英文身份。", code="mapping_required"))
        season_year = season["season_start_year"]
        cache_key = f"api_football_scope_v1:{team_id}:{season_year}"
        saved = self.store.get_meta(cache_key, {})
        if isinstance(saved, dict) and saved.get("local_team_name") == local_name \
                and saved.get("local_league_name") == league["english_name"] \
                and saved.get("local_season_id") == season["id"] \
                and _identifier((saved.get("scope") or {}).get("team_id")) \
                and _identifier((saved.get("scope") or {}).get("league_id")):
            return self.envelope(Capability.SQUAD, items=[_scope_item(saved)],
                                 cached=True, spent=Cost.FREE)

        spent = False
        league_params = {"country": identity[2], "type": "league"}
        try:
            leagues, league_cached, _ = await self._get("leagues", league_params)
            spent = not league_cached
            choices = []
            for entry in leagues["response"]:
                if not isinstance(entry, dict):
                    continue
                remote, country = entry.get("league") or {}, entry.get("country") or {}
                if (_exact_name(remote.get("name")) in {_exact_name(name) for name in identity[3]}
                        and _exact_name(country.get("name")) == _exact_name(identity[2])
                        and remote.get("type") == "League"
                        and _identifier(remote.get("id")) is not None
                        and any(_season(row.get("year")) == season_year
                                for row in entry.get("seasons") or [] if isinstance(row, dict))):
                    choices.append(remote)
            if len(choices) != 1:
                raise ApiFootballFailure("联赛名称或赛季未唯一匹配。",
                                         code="ambiguous_identity" if choices else "mapping_required",
                                         spent=spent)
            league_native = _identifier(choices[0]["id"])
            team_params = {"league": league_native, "season": season_year}
            teams, team_cached, fetched_at = await self._get("teams", team_params)
            spent = spent or not team_cached
            matches = []
            for entry in teams["response"]:
                remote = (entry or {}).get("team") if isinstance(entry, dict) else None
                if isinstance(remote, dict) and _identifier(remote.get("id")) is not None \
                        and _exact_name(remote.get("name")) in names:
                    matches.append(remote)
            if len(matches) != 1:
                raise ApiFootballFailure("球队英文名称未唯一匹配。",
                                         code="ambiguous_identity" if matches else "mapping_required",
                                         spent=spent)
            scope = {"team_id": _identifier(matches[0]["id"]),
                     "league_id": league_native, "season": season_year}
            record = {"scope": scope, "local_team_id": team_id,
                      "local_team_name": local_name, "local_league_name": league["english_name"],
                      "local_season_id": season["id"], "matched_team_name": matches[0]["name"],
                      "matched_league_name": choices[0]["name"], "fetched_at": fetched_at,
                      "source_urls": [f"{API_BASE}/leagues?{urlencode(league_params)}",
                                      f"{API_BASE}/teams?{urlencode(team_params)}"]}
            self.store.set_meta(cache_key, record)
            return self.envelope(Capability.SQUAD, items=[_scope_item(record)],
                                 cached=league_cached and team_cached,
                                 spent=Cost.QUOTA if spent else Cost.FREE)
        except ApiFootballFailure as exc:
            exc.spent = exc.spent or spent
            return self._gap(Capability.SQUAD, exc)

    async def _get(self, endpoint: str, params: dict):
        """Coalesce same-URL calls for this Store before reserving quota."""
        if self.store is None:
            return await self._get_unshared(endpoint, params)
        from ..research_transport import shared_request
        url = f"{API_BASE}/{endpoint}?{urlencode(params)}"
        return await shared_request(
            self.store, (self.source.id, url), API_HOST,
            lambda: self._get_unshared(endpoint, params),
        )

    async def _get_unshared(self, endpoint: str, params: dict):
        from ..ingestion import fetch_document, source_error_details
        from ..research_transport import blocking_request

        if self.store is None:
            raise ApiFootballFailure("读取来源需要持久存储。", code="no_store")
        try:
            key = self.credentials()
        except ProviderError:
            raise ApiFootballFailure("API-Football 尚未配置密钥。", code="missing_credential") from None
        url = f"{API_BASE}/{endpoint}?{urlencode(params)}"
        cache_key = "api_football_v1:" + url
        cached = self.store.get_meta(cache_key, {})
        if isinstance(cached, dict) and _fresh(cached.get("fetched_at"), CACHE_SECONDS[endpoint]):
            return cached["payload"], True, cached["fetched_at"]

        # Pace before reserving. A request queued behind another one must see
        # a 429 pause recorded by its predecessor when it reaches the gate.
        await _pace(self.pacing.enabled)
        quota = DailyQuota(self.store, self.source.id)
        try:
            reservation = quota.reserve()
        except Exception:
            raise ApiFootballFailure("配额存储暂时不可用，已停止请求。", code="quota_storage_unavailable",
                                     retryable=True) from None
        if not reservation.allowed:
            raise ApiFootballFailure("API-Football 今日配额已用尽或上游正在限流。",
                                     code="quota_exhausted", retryable=True,
                                     next_available_at=reservation.next_available_at)

        # Reservation is committed before network I/O. A failure, cancellation,
        # or process exit cannot refund a request that might have reached API.
        try:
            body, final_url, _ = await blocking_request(
                fetch_document, url, allowed_hosts={API_HOST}, max_body=MAX_BODY,
                content_types=("application/json", "text/json"),
                headers={"x-apisports-key": key}, timeout=25,
            )
            parts = urlsplit(final_url)
            if parts.scheme != "https" or parts.hostname != API_HOST or parts.fragment:
                raise ValueError("unexpected_response_url")
            if key.encode() in body:
                raise ValueError("credential_echoed")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("invalid_response")
            if payload.get("errors"):
                # API error details are not echoed: they may include request data.
                error_text = json.dumps(payload["errors"], ensure_ascii=False).lower()
                if "limit" in error_text or "rate" in error_text or "quota" in error_text:
                    duration = WINDOW if _daily_limit(error_text) else timedelta(minutes=1)
                    next_at = quota.pause_for_rate_limit(duration=duration)
                    raise ApiFootballFailure("API-Football 已限流。", code="http_rate_limited",
                                             spent=True, retryable=True, next_available_at=next_at)
                raise ApiFootballFailure("API-Football 拒绝了本次请求。", code="upstream_rejected",
                                         spent=True, retryable=True)
            if not isinstance(payload.get("response"), list):
                raise ValueError("invalid_response")
            paging = payload.get("paging") or {}
            if not isinstance(paging, dict) or _identifier(paging.get("total", 1)) != 1:
                raise ApiFootballFailure("来源资料有多页，尚未取得完整结果。", code="incomplete_page",
                                         spent=True, retryable=True)
            fetched_at = _now()
            self.store.set_meta(cache_key, {"payload": payload, "fetched_at": fetched_at})
            return payload, False, fetched_at
        except ApiFootballFailure:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            details = source_error_details(exc)
            if details.get("code") == "http_rate_limited":
                next_at = quota.pause_for_rate_limit()
                raise ApiFootballFailure("API-Football 已限流。", code="http_rate_limited",
                                         spent=True, retryable=True, next_available_at=next_at) from None
            raise ApiFootballFailure(details.get("message") or "API-Football 暂时不可用。",
                                     code=details.get("code") or "upstream_unavailable",
                                     spent=True, retryable=True) from None

    async def squad(self, team_id):
        team = _identifier(team_id)
        if team is None:
            return self._gap(Capability.SQUAD, ApiFootballFailure(
                "需要 API-Football 球队 ID；不能使用其他来源的球队编号。", code="provider_id_required"))
        try:
            payload, cached, fetched = await self._get("players/squads", {"team": team})
        except ApiFootballFailure as exc:
            return self._gap(Capability.SQUAD, exc)
        response = payload["response"]
        if len(response) != 1 or not isinstance(response[0], dict) or \
                _identifier((response[0].get("team") or {}).get("id")) != team:
            return self._gap(Capability.SQUAD, ApiFootballFailure(
                "来源未返回对应球队的阵容。", code="identity_mismatch", spent=not cached))
        players = response[0].get("players")
        if not isinstance(players, list) or not players:
            return self._gap(Capability.SQUAD, ApiFootballFailure(
                "来源未返回阵容。", code="empty_response", spent=not cached))
        items = []
        for player in players:
            if not isinstance(player, dict) or _identifier(player.get("id")) is None:
                continue
            fields = {name: player.get(name) for name in ("name", "number", "position", "age")}
            claims = {name: self._claim(name, value, fetched) for name, value in fields.items()
                      if value not in (None, "")}
            items.append({"provider_player": player["id"], "provider_team": team, "claims": claims})
        return self.envelope(Capability.SQUAD, items=items, cached=cached,
                             spent=Cost.FREE if cached else Cost.QUOTA)

    async def fixtures(self, league_id, season, team_id=None):
        return await self._league_answer(Capability.FIXTURE, "fixtures", league_id, season, team_id)

    async def standings(self, league_id, season, team_id=None):
        return await self._league_answer(Capability.STANDINGS, "standings", league_id, season, team_id)

    async def _league_answer(self, capability, endpoint, league_id, season, team_id=None):
        league, year, team = _identifier(league_id), _season(season), _identifier(team_id)
        if league is None or year is None or (team_id is not None and team is None):
            return self._gap(capability, ApiFootballFailure(
                "需要 API-Football 联赛 ID 和明确的赛季起始年。", code="provider_scope_required"))
        try:
            params = {"league": league, "season": year}
            if team is not None and capability is Capability.FIXTURE:
                params["team"] = team
            payload, cached, fetched = await self._get(endpoint, params)
        except ApiFootballFailure as exc:
            return self._gap(capability, exc)
        if capability is Capability.FIXTURE:
            items = self._fixtures(payload["response"], league, year, fetched)
        else:
            items = self._standings(payload["response"], league, year, fetched)
        if team is not None:
            items = [item for item in items if (team in (item["home_provider_team"],
                      item["away_provider_team"]) if capability is Capability.FIXTURE
                      else item["provider_team"] == team)]
        if not items:
            return self._gap(capability, ApiFootballFailure(
                "来源未返回可确认归属的资料。", code="empty_or_mismatched", spent=not cached))
        return self.envelope(capability, items=items, cached=cached,
                             spent=Cost.FREE if cached else Cost.QUOTA)

    def _fixtures(self, rows, league, season, fetched):
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            scope = row.get("league") or {}
            fixture = row.get("fixture") or {}
            if _identifier(scope.get("id")) != league or _season(scope.get("season")) != season \
                    or _identifier(fixture.get("id")) is None:
                continue
            teams = row.get("teams") or {}
            home, away = teams.get("home") or {}, teams.get("away") or {}
            if _identifier(home.get("id")) is None or _identifier(away.get("id")) is None:
                continue
            goals = row.get("goals") or {}
            score = None if goals.get("home") is None or goals.get("away") is None \
                else f"{goals['home']}-{goals['away']}"
            fields = {"kickoff": fixture.get("date"), "status": (fixture.get("status") or {}).get("short"),
                      "score": score}
            items.append({"provider_fixture": fixture["id"], "provider_league": league,
                          "season": season, "home_provider_team": home["id"],
                          "away_provider_team": away["id"], "home": home.get("name"),
                          "away": away.get("name"), "claims": {
                              name: self._claim(name, value, fetched) for name, value in fields.items()
                              if value not in (None, "")}})
        return items

    def _standings(self, rows, league, season, fetched):
        items = []
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            scope = entry.get("league") or {}
            if _identifier(scope.get("id")) != league or _season(scope.get("season")) != season:
                continue
            for group in scope.get("standings") or []:
                if not isinstance(group, list):
                    continue
                for row in group:
                    if not isinstance(row, dict):
                        continue
                    team = row.get("team") or {}
                    if _identifier(team.get("id")) is None or not isinstance(row.get("rank"), int):
                        continue
                    items.append({"provider_team": team["id"], "name": team.get("name"),
                                  "provider_league": league, "season": season,
                                  "group": row.get("group"), "rank": row["rank"],
                                  "played": (row.get("all") or {}).get("played"),
                                  "points": row.get("points"),
                                  "claim": self._claim("standings", row["rank"], fetched,
                                                       observed_at=row.get("update"))})
        return items

    def _claim(self, field, value, fetched, *, observed_at=None):
        return provenance.claim_from(
            {"id": self.source.id, "url": API_BASE + "/", "name": self.source.name},
            field, value, fetched_at=fetched, observed_at=observed_at or "",
        )


def _identifier(value):
    if isinstance(value, bool) or not str(value or "").isdigit():
        return None
    number = int(value)
    return number if number > 0 else None


def _season(value):
    number = _identifier(value)
    return number if number is not None and 2000 <= number <= 2100 else None


def _now():
    return datetime.now(timezone.utc).isoformat()


def _fresh(stamp, seconds):
    try:
        value = datetime.fromisoformat(stamp)
        age = (datetime.now(timezone.utc) - value).total_seconds()
        return value.tzinfo is not None and 0 <= age < seconds
    except (TypeError, ValueError):
        return False


def _exact_name(value):
    return " ".join(value.split()).casefold() if isinstance(value, str) else ""


def _english_alias(value):
    return isinstance(value, str) and bool(value.strip()) and not any(
        "\u3400" <= character <= "\u9fff" for character in value)


def _scope_item(record):
    return {key: record[key] for key in (
        "scope", "local_team_id", "matched_team_name", "matched_league_name",
        "fetched_at", "source_urls")}


async def resolve_team_scope(store, team_id, year=None):
    return await ApiFootballProvider(store=store).resolve_team_scope(team_id, year)


def _daily_limit(text):
    return any(token in text for token in ("daily", "per day", "the day", "24h", "24 hour"))


async def _pace(enabled):
    """Reserve one process-wide time slot before each actual API request."""
    if not enabled:
        return
    global _NEXT_REQUEST_AT
    with _PACE_LOCK:
        now = time.monotonic()
        slot = max(now, _NEXT_REQUEST_AT)
        _NEXT_REQUEST_AT = slot + MIN_INTERVAL_SECONDS
    if slot > now:
        await asyncio.sleep(slot - now)
