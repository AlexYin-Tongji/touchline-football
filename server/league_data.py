"""Season-scoped domestic leagues and checked European team fixtures from ESPN.

The provider is a sports publisher, not an official league feed. No credential,
embedded private $ref, or arbitrary URL is followed. Failed snapshots preserve
the previously checked data and its original timestamps.
"""
from __future__ import annotations

import asyncio
import copy
import json
import hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit
from urllib.request import getproxies

import httpx

from .ingestion import IngestionError, now, published_time
from .league_catalog import EUROPEAN_COMPETITIONS

API = "https://site.api.espn.com"
MAX_BODY = 16 * 1024 * 1024
CACHE_SECONDS = 1800


async def fetch_json(url):
    if urlsplit(url).hostname != "site.api.espn.com" or not url.startswith(API + "/apis/"):
        raise IngestionError("比赛数据仅可访问固定的公开数据来源。")
    try:
        async with asyncio.timeout(32):
            # httpx does not read Windows' registry proxy. Use the same
            # explicitly configured system HTTPS proxy as the news transport;
            # loopback and arbitrary hosts never enter this fixed-source path.
            async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=5), follow_redirects=False,
                                         proxy=getproxies().get("https"), trust_env=False,
                                         headers={"User-Agent": "Touchline/6.0 (personal football reader)", "Accept": "application/json"}) as client:
                for attempt in range(2):
                    if attempt:
                        await asyncio.sleep(.25)
                    try:
                        async with client.stream("GET", url) as response:
                            if response.status_code != 200:
                                if response.status_code == 403:
                                    from .espn_pages import fallback_spec, fetch_public_payload
                                    spec = fallback_spec(url)
                                    if spec:
                                        return await fetch_public_payload(spec)
                                # Retry a transient failure once within the same
                                # total deadline; never retry a denied source or
                                # malformed/season-mismatched payload.
                                if attempt == 0 and (response.status_code == 429 or response.status_code >= 500):
                                    continue
                                raise IngestionError(f"ESPN 数据源返回 HTTP {response.status_code}，已保留上次数据。")
                            if "json" not in response.headers.get("content-type", ""):
                                raise IngestionError("比赛来源没有返回可识别的数据格式。")
                            chunks, size = [], 0
                            async for chunk in response.aiter_bytes():
                                size += len(chunk)
                                if size > MAX_BODY:
                                    raise IngestionError("比赛数据超过本次读取上限，未保存不完整数据。")
                                chunks.append(chunk)
                            return json.loads(b"".join(chunks))
                    except httpx.TransportError:
                        if attempt:
                            from .espn_pages import fallback_spec, fetch_public_payload
                            spec = fallback_spec(url)
                            if spec:
                                return await fetch_public_payload(spec)
                            raise
    except (httpx.TimeoutException, TimeoutError):
        raise IngestionError("比赛来源响应超时，已保留上次数据。") from None
    except IngestionError:
        raise
    except (httpx.HTTPError, ValueError):
        raise IngestionError("比赛数据暂不可读，请稍后重试。") from None


def season_context(store, league_id, year=None):
    league = store.get("leagues", league_id)
    if not league:
        raise IngestionError("联赛不存在。")
    seasons = [item for item in store.list("seasons") if item["league_id"] == league_id]
    candidates = ([s for s in seasons if s["season_start_year"] == year] if year is not None
                  else sorted(seasons, key=lambda s: s["season_start_year"], reverse=True))
    if not candidates:
        raise IngestionError("这个赛季尚未建立可核对的参赛球队目录。")
    members = store.list("league_memberships")
    for season in candidates:
        mapping = {str(m["provider_team_id"]): m["team_id"] for m in members if m["season_id"] == season["id"]}
        if len(mapping) == league["expected_team_count"]:
            return league, season, mapping
    raise IngestionError("该联赛的赛季球队目录不完整，暂不导入比赛。")


def _number(value, *, signed=False):
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        if not number.is_integer() or (number < 0 and not signed) or abs(number) > 10000:
            return None
        return int(number)
    except (ValueError, TypeError, OverflowError):
        return None


def parse_scoreboard(payload, league, season, mapping):
    headers = payload.get("leagues") or []
    header = next((row for row in headers if row.get("slug") == league["provider_code"]), None)
    if not header or (header.get("season") or {}).get("year") != season["season_start_year"]:
        raise IngestionError("赛程来源的联赛或赛季与所选目录不一致。")
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        raise IngestionError("来源未返回赛程，不会用空表覆盖已有比赛。")
    result = {}
    for event in events:
        if (event.get("season") or {}).get("year") != season["season_start_year"]:
            raise IngestionError("赛程响应混入了其他赛季。")
        competitions = event.get("competitions") or []
        if len(competitions) != 1:
            raise IngestionError("比赛记录无法确认唯一对阵。")
        game = competitions[0]
        competitors = game.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if len(competitors) != 2 or not home or not away or any(c.get("type") != "team" for c in competitors):
            raise IngestionError("比赛参赛方结构不完整。")
        h_id, a_id = str(home.get("id")), str(away.get("id"))
        if h_id not in mapping or a_id not in mapping or h_id == a_id:
            raise IngestionError("比赛包含不属于该赛季的球队，未导入。")
        provider_id = str(event.get("id", ""))
        if not provider_id.isdigit():
            raise IngestionError("比赛缺少可追溯的来源编号。")
        typ = (game.get("status") or event.get("status") or {}).get("type") or {}
        label = str(typ.get("name", "")).upper()
        state = "scheduled"
        if any(word in label for word in ("POSTPONED", "SUSPENDED", "ABANDONED")):
            state = "postponed"
        elif "CANCEL" in label:
            state = "cancelled"
        elif typ.get("completed") is True and typ.get("state") == "post":
            state = "finished"
        elif typ.get("state") == "in":
            state = "live"
        kickoff = published_time(game.get("date") or event.get("date"))
        confirmed = bool(kickoff and game.get("timeValid", event.get("timeValid", False)) is True and state not in {"postponed", "cancelled"})
        hs, aws = (_number(home.get("score")), _number(away.get("score"))) if state == "finished" else (None, None)
        if state == "finished" and (hs is None or aws is None):
            raise IngestionError("已完赛比赛缺少有效比分，保留上次完整赛程。")
        record = {"id": f"espn-{league['id']}-{season['season_start_year']}-{provider_id}", "league_id": league["id"],
                  "competition": league.get("name", league["id"]),
                  "season_id": season["id"], "season_start_year": season["season_start_year"], "provider": "ESPN",
                  "provider_match_id": provider_id, "home_team_id": mapping[h_id], "away_team_id": mapping[a_id],
                  "home_team": (home.get("team") or {}).get("displayName", ""), "away_team": (away.get("team") or {}).get("displayName", ""),
                  "kickoff": kickoff or "", "kickoff_date": (kickoff or str(event.get("date") or ""))[:10],
                  "kickoff_confirmed": confirmed, "status": state, "home_score": hs, "away_score": aws,
                  "stadium": (game.get("venue") or {}).get("fullName", ""),
                  "source_url": f"https://www.espn.com/soccer/match/_/gameId/{provider_id}", "fetched_at": now()}
        if record["id"] in result:
            previous = {k: v for k, v in result[record["id"]].items() if k != "fetched_at"}
            if previous != {k: v for k, v in record.items() if k != "fetched_at"}:
                raise IngestionError("同一场比赛的来源记录互相冲突。")
        result[record["id"]] = record
    return list(result.values())


def parse_standings(payload, league, season, mapping):
    if (payload.get("season") or {}).get("year") != season["season_start_year"] or payload.get("uid") != league.get("provider_league_uid"):
        raise IngestionError("积分来源的联赛或赛季与当前目录不一致。")
    children = payload.get("children") or []
    entries = [entry for child in children for entry in (child.get("standings") or {}).get("entries", [])]
    rows = []
    fields = {"rank": "rank", "played": "gamesPlayed", "won": "wins", "drawn": "ties", "lost": "losses", "goals_for": "pointsFor", "goals_against": "pointsAgainst", "goal_difference": "pointDifferential", "points": "points"}
    for entry in entries:
        provider_id = str((entry.get("team") or {}).get("id"))
        if provider_id not in mapping:
            raise IngestionError("积分榜包含赛季目录之外的球队。")
        stats = {value.get("name"): value.get("value") for value in entry.get("stats", [])}
        row = {field: _number(stats.get(stat), signed=field in {"goal_difference", "points"}) for field, stat in fields.items()}
        if any(value is None for value in row.values()) or row["played"] != row["won"] + row["drawn"] + row["lost"] or row["goal_difference"] != row["goals_for"] - row["goals_against"]:
            raise IngestionError("积分榜字段缺失或比赛 / 净胜球计算不一致。")
        # Points may include disciplinary deductions; retain the provider's value.
        rows.append({"team_id": mapping[provider_id], **row})
    expected = league["expected_team_count"]
    if len(rows) != expected or len({r["team_id"] for r in rows}) != expected or {r["rank"] for r in rows} != set(range(1, expected + 1)):
        raise IngestionError("积分榜球队或排名不完整，保留上次完整数据。")
    return sorted(rows, key=lambda row: row["rank"])


def _sync_key(season_id):
    return "league_sync_" + season_id


def team_schedule_key(season_id, team_id):
    return f"team_schedule_{season_id}_{team_id}"


def _elapsed(value):
    parsed = published_time(value)
    return (datetime.now(timezone.utc) - datetime.fromisoformat(parsed)).total_seconds() if parsed else float("inf")


async def sync_standings(store, league_id, year=None, *, force=False):
    """Share the small standings request between clubs, independently of fixtures."""
    league, season, mapping = season_context(store, league_id, year)
    key = "league_standings_" + season["id"]
    if not hasattr(store, "_standings_locks"):
        store._standings_locks = {}
    async with store._standings_locks.setdefault(key, asyncio.Lock()):
        prior = store.get_meta(key, {})
        table = store.get("standings", season["id"]) or {}
        if (_elapsed(prior.get("last_attempt")) < 60 or
                not force and prior.get("sync_status") != "failed" and table.get("rows") and _elapsed(table.get("fetched_at")) < CACHE_SECONDS):
            return {**prior, "sync_status": prior.get("sync_status", "completed"), "cached": True}
        attempt = now()
        store.set_meta(key, {**prior, "sync_status": "running", "last_attempt": attempt, "errors": []})
        try:
            payload = await fetch_json(f"{API}/apis/v2/sports/soccer/{league['provider_code']}/standings?season={season['season_start_year']}")
            rows = parse_standings(payload, league, season, mapping)
            stamp = now()
            store.put("standings", {"id": season["id"], "league_id": league_id, "season_id": season["id"],
                      "rows": rows, "fetched_at": stamp,
                      "source_method": (payload.get("_touchline_source") or {}).get("method", "api"),
                      "source_url": f"https://www.espn.com/soccer/standings/_/league/{league['provider_code']}/season/{season['season_start_year']}"})
            result = {"sync_status": "completed", "last_sync": stamp, "last_attempt": attempt, "errors": []}
        except asyncio.CancelledError:
            store.set_meta(key, {**prior, "sync_status": "interrupted", "last_attempt": attempt, "errors": ["积分更新中断，已保留上次资料。"]})
            raise
        except Exception as exc:
            result = {**prior, "sync_status": "failed", "last_attempt": attempt,
                      "errors": [str(exc) if isinstance(exc, IngestionError) else "积分来源暂不可用，已保留上次资料。"]}
        store.set_meta(key, result)
        return result


def parse_team_schedule(payload, league, season, mapping, provider_team_id, *, include_continental=False, provider_mapping=None):
    """Validate the fixed team's feed, then reuse the normal match parser.

    Domestic opponents must belong to the reviewed season directory. Optional
    European games require explicit competition and provider-team identities;
    other cups are skipped and missing/contradictory identity fails the import.
    """
    if not isinstance(payload, dict):
        raise IngestionError("球队赛程格式无法识别，已保留旧记录。")
    expected_year = season["season_start_year"]
    for field in ("season", "requestedSeason"):
        year = (payload.get(field) or {}).get("year")
        if type(year) is not int or year != expected_year:
            raise IngestionError("球队赛程没有确认所选赛季，已保留旧记录。")
    identity = str(provider_team_id)
    team = payload.get("team") or {}
    if not identity.isdigit() or identity not in mapping or str(team.get("id")) != identity:
        raise IngestionError("球队赛程的来源编号不匹配，已保留旧记录。")
    expected_label = f"{expected_year}-{str(expected_year + 1)[-2:]}"
    if team.get("seasonSummary") and team["seasonSummary"] not in {expected_label, season["label"]}:
        raise IngestionError("球队赛程的赛季标签互相冲突。")
    events = payload.get("events")
    if not isinstance(events, list) or len(events) > 250:
        raise IngestionError("球队赛程没有返回可读取的比赛列表。")
    selected, continental = [], []
    league_provider_id = str(league.get("provider_league_uid", "")).rsplit(":", 1)[-1]
    for event in events:
        if not isinstance(event, dict):
            raise IngestionError("球队赛程包含格式不完整的记录。")
        competition_league = event.get("league") or {}
        slug = competition_league.get("slug")
        if not slug:
            raise IngestionError("球队赛程缺少可核对的赛事归属。")
        if slug != league["provider_code"]:
            if include_continental and slug in EUROPEAN_COMPETITIONS:
                competition = EUROPEAN_COMPETITIONS[slug]
                competitors = [row for game in event.get("competitions", []) for row in game.get("competitors", [])]
                if (len(competitors) != 2 or not any(str(row.get("id")) == identity for row in competitors)
                        or any(not str(row.get("id", "")).isdigit() or not (row.get("team") or {}).get("displayName") for row in competitors)):
                    raise IngestionError("欧战赛程缺少可核对的球队编号与参赛关系。")
                foreign_mapping = {str(row["id"]): (provider_mapping or {}).get(str(row["id"]), mapping.get(str(row["id"]), "espn-team-" + str(row["id"]))) for row in competitors}
                foreign_season = {**season, "id": f"{competition['id']}-{expected_year}"}
                continental.extend(parse_scoreboard({"leagues": [{"slug": slug, "season": {"year": expected_year}}],
                    "events": [event]}, competition, foreign_season, foreign_mapping))
            continue
        if competition_league.get("id") is not None and league_provider_id.isdigit() and str(competition_league["id"]) != league_provider_id:
            raise IngestionError("球队赛程的联赛编号互相冲突。")
        competitors = [row for game in event.get("competitions", []) for row in game.get("competitors", [])]
        if not any(str(row.get("id")) == identity for row in competitors):
            raise IngestionError("球队赛程混入了另一支球队的比赛。")
        selected.append(event)
    if not selected and not continental:
        raise IngestionError("球队来源暂未返回本赛季的联赛比赛，已保留旧记录。")
    # Header identity is established above from the fixed request, the response
    # team/season and each event's explicit league. No score is synthesized.
    records = (parse_scoreboard({"leagues": [{"slug": league["provider_code"], "season": {"year": expected_year}}],
                               "events": selected}, league, season, mapping) if selected else []) + continental
    source = payload.get("_touchline_source") or {}
    if source.get("method") == "espn_public_page":
        for record in records:
            record.update(source_method="espn_public_page", source_page_url=source.get("url"))
    return records


async def sync_team_recent(store, team_id, year=None, *, force=False):
    """Supplement one team's matches without replacing the full league snapshot."""
    memberships = [m for m in store.list("league_memberships") if m.get("team_id") == team_id
                   and (year is None or m.get("season_start_year") == year)]
    if not memberships:
        raise IngestionError("球队没有可核对的赛季目录。")
    membership = max(memberships, key=lambda item: item["season_start_year"])
    league, season, mapping = season_context(store, membership["league_id"], membership["season_start_year"])
    provider_id = str(membership.get("provider_team_id", ""))
    if not provider_id.isdigit() or mapping.get(provider_id) != team_id:
        raise IngestionError("球队来源编号与赛季目录不一致。")
    key = team_schedule_key(season["id"], team_id)
    if not hasattr(store, "_team_schedule_locks"):
        store._team_schedule_locks = {}
    async with store._team_schedule_locks.setdefault(key, asyncio.Lock()):
        prior = store.get_meta(key, {})
        if _elapsed(prior.get("last_attempt")) < 60:
            return {**prior, "cached": True}
        previous_parts = prior.get("parts", {})
        needed = [part for part in ("results", "upcoming") if force
                  or previous_parts.get(part, {}).get("status") in {"failed", "interrupted"}
                  or _elapsed(prior.get(part + "_updated_at")) >= CACHE_SECONDS]
        if not needed:
            return {**prior, "cached": True}
        attempt_at = now()
        source_url = f"https://www.espn.com/soccer/team/results/_/id/{provider_id}/league/all/season/{season['season_start_year']}"
        upcoming_source_url = f"https://www.espn.com/soccer/team/fixtures/_/id/{provider_id}/league/all/season/{season['season_start_year']}"
        parts = copy.deepcopy(previous_parts)
        for part in needed:
            parts[part] = {**parts.get(part, {}), "status": "running", "last_attempt": attempt_at, "errors": []}
        store.set_meta(key, {**prior, "sync_status": "running", "last_attempt": attempt_at, "errors": [], "parts": parts})
        url = f"{API}/apis/site/v2/sports/soccer/all/teams/{provider_id}/schedule?season={season['season_start_year']}"
        provider_mapping = {str(row["provider_team_id"]): row["team_id"] for row in store.list("league_memberships")
                            if row.get("season_start_year") == season["season_start_year"]}
        try:
            responses = await asyncio.gather(*(fetch_json(url + ("&fixture=true" if part == "upcoming" else "")) for part in needed), return_exceptions=True)
            combined, errors, checked = {}, [], {}
            for part, payload in zip(needed, responses):
                try:
                    if isinstance(payload, BaseException):
                        raise payload
                    parsed = parse_team_schedule(payload, league, season, mapping, provider_id,
                                                 include_continental=True, provider_mapping=provider_mapping)
                    candidate = dict(combined)
                    for row in parsed:
                        previous = candidate.get(row["id"])
                        if previous:
                            # The game may finish between these two responses;
                            # a results entry takes precedence over its stale
                            # scheduled counterpart, never the reverse.
                            if previous["status"] == "finished" and row["status"] != "finished":
                                continue
                            if row["status"] == "finished" and previous["status"] != "finished":
                                candidate[row["id"]] = row
                                continue
                            if any(previous.get(field) != row.get(field) for field in ("home_team_id", "away_team_id", "status", "home_score", "away_score", "kickoff")):
                                raise IngestionError("球队两个赛程来源中的同一比赛互相冲突。")
                        candidate[row["id"]] = row
                    combined = candidate
                    checked[part] = now()
                    parts[part] = {"status": "completed", "updated_at": checked[part], "last_attempt": attempt_at,
                                   "snapshot_match_ids": [row["id"] for row in parsed], "errors": []}
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    message = ("已赛：" if part == "results" else "未来赛程：") + (str(exc) if isinstance(exc, IngestionError) else "来源暂不可用，已保留旧记录。")
                    errors.append(message)
                    parts[part] = {**previous_parts.get(part, {}), "status": "failed", "last_attempt": attempt_at, "errors": [message]}
            records = list(combined.values())
            if not records:
                raise IngestionError("；".join(errors) or "球队赛程暂未返回有效比赛，已保留旧记录。")
            if not hasattr(store, "_league_locks"):
                store._league_locks = {}
            # Serialize snapshot-ID merges with the full-league writer. A
            # concurrent refresh must not discard either writer's new matches.
            async with store._league_locks.setdefault(season["id"], asyncio.Lock()):
                existing = {row["id"]: row for row in store.list("league_matches") if row.get("season_id") == season["id"]
                            or (row.get("season_start_year") == season["season_start_year"] and team_id in {row.get("home_team_id"), row.get("away_team_id")})}
                for row in records:
                    old = existing.get(row["id"])
                    if old and old.get("status") == "finished" and row["status"] != "finished":
                        raise IngestionError("球队来源把已完赛比赛退回未开赛，已保留旧记录。")
                    if old and (old.get("home_team_id"), old.get("away_team_id")) != (row["home_team_id"], row["away_team_id"]):
                        raise IngestionError("球队赛程与已有比赛的对阵不一致，已保留旧记录。")
                    row.setdefault("source_method", "team_schedule")
                league_status = store.get_meta(_sync_key(season["id"]), {})
                ids = list(dict.fromkeys([*league_status.get("snapshot_match_ids", [row["id"] for row in existing.values() if row.get("season_id") == season["id"]]),
                                          *(row["id"] for row in records if row["season_id"] == season["id"])]))
                store.put_many("league_matches", records)
                # Deliberately retain full-league errors and timestamps. Only
                # this team's checked matches were refreshed by this endpoint.
                store.set_meta(_sync_key(season["id"]), {**league_status, "snapshot_match_ids": ids,
                               "fixture_count": len(ids)})
                for record_season in {season["id"], *(row["season_id"] for row in records)}:
                    materialize_team(store, team_id, record_season)
            selected_ids = set()
            for part in ("results", "upcoming"):
                if "snapshot_match_ids" in parts.get(part, {}):
                    selected_ids.update(parts[part]["snapshot_match_ids"])
                else:
                    # Older installs have only a league snapshot. Retain its
                    # scoped records until this part gets a checked response.
                    selected_ids.update(row["id"] for row in existing.values() if row["id"] in ids
                        and team_id in {row.get("home_team_id"), row.get("away_team_id")}
                        and (row.get("status") == "finished") == (part == "results"))
            selected_records = [row for row in store.list("league_matches") if row["id"] in selected_ids]
            result = {"team_id": team_id, "season_id": season["id"], "sync_status": "partial" if errors else "completed", "errors": errors,
                      "last_attempt": attempt_at, "last_sync": now(), "source_url": source_url,
                      "upcoming_source_url": upcoming_source_url,
                      "results_updated_at": checked.get("results") or prior.get("results_updated_at"),
                      "upcoming_updated_at": checked.get("upcoming") or prior.get("upcoming_updated_at"),
                      "match_count": len(selected_records), "completed_match_count": sum(row["status"] == "finished" for row in selected_records),
                      "scope": "team_schedule", "provider": "ESPN", "parts": parts,
                      "snapshot_match_ids": sorted(selected_ids)}
        except asyncio.CancelledError:
            store.set_meta(key, {**prior, "sync_status": "interrupted", "last_attempt": attempt_at, "errors": ["球队赛程更新中断，已保留旧记录。"]})
            raise
        except Exception as exc:
            # A failed atomic merge cannot mark either response as refreshed.
            parts = {**previous_parts, **{part: {**previous_parts.get(part, {}), "status": "failed", "last_attempt": attempt_at,
                     "errors": [str(exc) if isinstance(exc, IngestionError) else "来源暂不可用。"]} for part in needed}}
            result = {**prior, "team_id": team_id, "season_id": season["id"], "sync_status": "failed", "last_attempt": attempt_at, "parts": parts,
                      "errors": [str(exc) if isinstance(exc, IngestionError) else "球队赛程暂不可用，已保留旧记录。"]}
        store.set_meta(key, result)
        return result


async def sync_league(store, league_id, year=None, *, force=False):
    _, season, _ = season_context(store, league_id, year)
    if not hasattr(store, "_league_locks"):
        store._league_locks = {}
    lock = store._league_locks.setdefault(season["id"], asyncio.Lock())
    async with lock:
        return await _sync_league(store, league_id, year, force=force)


async def _sync_league(store, league_id, year=None, *, force=False):
    league, season, mapping = season_context(store, league_id, year)
    key = _sync_key(season["id"])
    prior = store.get_meta(key, {})
    last = published_time(prior.get("last_sync"))
    if last and not force and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < CACHE_SECONDS:
        return {**prior, "cached": True}
    store.set_meta(key, {**prior, "sync_status": "running", "errors": []})
    code, year = league["provider_code"], season["season_start_year"]
    fixtures_url = f"{API}/apis/site/v2/sports/soccer/{code}/scoreboard?dates={year}0601-{year+1}0601&limit=1000"
    table_url = f"{API}/apis/v2/sports/soccer/{code}/standings?season={year}"
    results = await asyncio.gather(fetch_json(fixtures_url), fetch_json(table_url), return_exceptions=True)
    errors, updated, part_status = [], 0, {}
    for kind, payload in zip(("fixtures", "standings"), results):
        try:
            if isinstance(payload, BaseException):
                raise payload
            if kind == "fixtures":
                records = parse_scoreboard(payload, league, season, mapping)
                existing = {r["id"]: r for r in store.list("league_matches") if r.get("season_id") == season["id"]}
                # A smaller successful response must not quietly appear as full
                # fresh coverage through leftover records from an older fetch.
                for row in records:
                    old = existing.get(row["id"])
                    if old and old.get("status") == "finished" and row["status"] != "finished":
                        raise IngestionError("来源把已完赛记录退回未开赛，保留上次赛程。")
                store.put_many("league_matches", records)
                prior["fixtures_at"], prior["fixture_count"] = now(), len(records)
                prior["snapshot_match_ids"] = [row["id"] for row in records]
            else:
                rows = parse_standings(payload, league, season, mapping)
                store.put("standings", {"id": season["id"], "league_id": league_id, "season_id": season["id"], "rows": rows, "fetched_at": now(), "source_url": f"https://www.espn.com/soccer/standings/_/league/{code}/season/{year}"})
                prior["standings_at"] = now()
            updated += 1
            part_status[kind + "_status"] = "completed"
        except asyncio.CancelledError:
            raise
        except (IngestionError, ValueError, TypeError, KeyError) as exc:
            part_status[kind + "_status"] = "failed"
            errors.append(("赛程：" if kind == "fixtures" else "积分：") + str(exc))
        except Exception:
            part_status[kind + "_status"] = "failed"
            errors.append(("赛程" if kind == "fixtures" else "积分") + "来源暂不可用，已保留上次数据。")
    status = {**prior, **part_status, "sync_status": "completed" if not errors else "partial" if updated else "failed", "errors": errors, "last_attempt": now()}
    if updated:
        status["last_sync"] = now()
    store.set_meta(key, status)
    # Expose full-team fixtures through the existing team UI/Agent interface.
    for team_id in store.settings().get("followed_teams", []):
        if team_id in mapping.values():
            materialize_team(store, team_id, season["id"])
    return status


def materialize_team(store, team_id, season_id=None):
    """Derive per-team match views; retain existing official/manual records."""
    records = [r for r in store.list("league_matches") if team_id in {r["home_team_id"], r["away_team_id"]} and (not season_id or r["season_id"] == season_id)]
    existing = store.list("matches", "live", team_id)
    output = []
    for source in records:
        home = source["home_team_id"] == team_id
        opponent_id = source["away_team_id"] if home else source["home_team_id"]
        opponent = store.get("teams", opponent_id) or {}
        opponent_name = opponent.get("name") or (source["away_team"] if home else source["home_team"])
        # The four direct club adapters remain authoritative when the opponent
        # and fixture date identify the same game. Do not combine their scores.
        names = {opponent_name, opponent.get("english_name"), source["away_team"] if home else source["home_team"]}
        if any(old.get("provider") != "ESPN" and old.get("opponent") in names and (old.get("kickoff") or "")[:10] == source["kickoff_date"] for old in existing):
            continue
        league = store.get("leagues", source["league_id"]) or {}
        gf, ga = (source["home_score"], source["away_score"]) if home else (source["away_score"], source["home_score"])
        identity = source["id"] + "-" + team_id
        old = store.get("matches", identity) or {}
        evidence_text = "ESPN 男足比赛资料（公开数据字段，非模型生成）\n" + json.dumps({k: v for k, v in source.items() if k != "fetched_at"}, ensure_ascii=False, indent=2)
        evidence_id = "ev_league_" + hashlib.sha256((team_id + evidence_text).encode()).hexdigest()[:22]
        if not store.get("evidence", evidence_id):
            store.put("evidence", {"id": evidence_id, "mode": "live", "team_id": team_id, "text": evidence_text,
                                   "title": f"{source['home_team']} vs {source['away_team']} · ESPN比赛资料", "fixture_record": True,
                                   "match_id": identity, "url": source["source_url"], "source_name": "ESPN 赛事数据", "origin": "web",
                                   "published_at": None, "fetched_at": source["fetched_at"], "spoiler": source["status"] in {"finished", "live"},
                                   "content_status": "full_text", "content_method": "structured_fixture", "fingerprint": hashlib.sha256(evidence_text.encode()).hexdigest()})
        updated = {**old, "id": identity, "mode": "live", "team_id": team_id, "opponent": opponent_name, "opponent_id": opponent_id,
                       "competition": source.get("competition") or league.get("name", source["league_id"]), "league_id": source["league_id"], "season_id": source["season_id"],
                       "provider": "ESPN", "venue": "home" if home else "away", "stadium": source["stadium"], "kickoff": source["kickoff"],
                       "kickoff_date": source["kickoff_date"], "kickoff_confirmed": source["kickoff_confirmed"],
                       "status": "finished" if source["status"] == "finished" else "scheduled", "fixture_status": source["status"],
                       "score": f"{gf}–{ga}" if gf is not None and ga is not None else None,
                       "home_team": source["home_team"], "away_team": source["away_team"], "home_score": source["home_score"], "away_score": source["away_score"],
                       "goals_for": gf, "goals_against": ga, "result": ("W" if gf > ga else "L" if gf < ga else "D") if gf is not None and ga is not None else None,
                       "source_match_id": source["provider_match_id"], "source_url": source["source_url"], "source_name": "ESPN 赛事数据",
                       "source_updated_at": source["fetched_at"], "note": old.get("note", ""), "evidence_ids": list(dict.fromkeys([*(eid for eid in old.get("evidence_ids", []) if not eid.startswith("ev_league_")), evidence_id])),
                       "lineups": old.get("lineups", {"status": "unavailable"}), "availability": old.get("availability", []),
                       "data_warnings": [] if source["kickoff_confirmed"] else ["开球时间尚未确认，以来源后续更新为准。"]}
        if updated != old:
            output.append(updated)
    if output:
        store.put_many("matches", output)
    return len(output)


async def sync_team(store, team_id, *, force=False):
    members = [m for m in store.list("league_memberships") if m["team_id"] == team_id]
    if not members:
        return {"matches_updated": 0, "evidence_added": 0, "lineups_updated": 0, "errors": ["此球队尚无已核对的五大联赛赛季资料。"]}
    member = max(members, key=lambda m: m["season_start_year"])
    result = await sync_team_recent(store, team_id, member["season_start_year"], force=force)
    count = materialize_team(store, team_id, member["season_id"])
    return {"matches_updated": count, "evidence_added": 0, "lineups_updated": 0, "synced_at": result.get("last_sync"), "errors": result.get("errors", []), "provider": "ESPN", "cached": result.get("cached", False)}


def league_view(store, league_id, year=None):
    league, season, mapping = season_context(store, league_id, year)
    status = store.get_meta(_sync_key(season["id"]), {})
    all_records = [r for r in store.list("league_matches") if r["season_id"] == season["id"]]
    snapshot_ids = set(status.get("snapshot_match_ids", [r["id"] for r in all_records]))
    records = [r for r in all_records if r["id"] in snapshot_ids]
    table = store.get("standings", season["id"]) or {}
    protected = store.settings().get("spoiler_free", True)
    teams = [store.get("teams", team_id) for team_id in mapping.values()]
    teams = [t for t in teams if t]
    fixtures = []
    for row in sorted(records, key=lambda r: r.get("kickoff") or "9999"):
        view = {key: row.get(key) for key in ("id", "home_team_id", "away_team_id", "home_team", "away_team", "kickoff", "kickoff_date", "kickoff_confirmed", "status", "home_score", "away_score", "source_url")}
        if protected:
            view.update(home_score=None, away_score=None, spoiler_hidden=True)
        fixtures.append(view)
    count = league["expected_team_count"]
    return {**league, "season_label": season["label"], "season_start_year": season["season_start_year"], "teams": teams, "team_count": len(teams),
            "coverage": {"teams": len(teams), "expected_teams": count, "matches": len(records), "expected_matches": count * (count - 1),
                         "finished": sum(r["status"] == "finished" for r in records), "scheduled": sum(r["status"] == "scheduled" for r in records),
                         "standings_rows": len(table.get("rows", [])), "has_full_schedule": len(records) == count * (count - 1) and len({(r['home_team_id'], r['away_team_id']) for r in records}) == count * (count - 1)},
            "last_sync": status.get("last_sync"), "fixtures_updated_at": status.get("fixtures_at"), "standings_updated_at": table.get("fetched_at"),
            "standings": [] if protected else table.get("rows", []), "standings_hidden": protected, "fixtures": fixtures,
            "errors": status.get("errors", []), "sync_status": status.get("sync_status", "idle"),
            "source_url": f"https://www.espn.com/soccer/standings/_/league/{league['provider_code']}/season/{season['season_start_year']}"}


class LeagueSyncManager:
    def __init__(self, store):
        self.store, self.task = store, None

    def start(self, league_id=None, year=None):
        if self.task and not self.task.done():
            return {"started": False, "status": "running"}
        ids = [league_id] if league_id else [league["id"] for league in self.store.list("leagues")]
        contexts = [season_context(self.store, identity, year) for identity in ids]
        for _, season, _ in contexts:
            key = _sync_key(season["id"])
            self.store.set_meta(key, {**self.store.get_meta(key, {}), "sync_status": "running", "errors": []})
        async def run():
            for league, season, _ in contexts:
                try:
                    await sync_league(self.store, league["id"], season["season_start_year"], force=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    key = _sync_key(season["id"])
                    self.store.set_meta(key, {**self.store.get_meta(key, {}), "sync_status": "failed", "errors": ["同步中断，请重试。"]})
        self.task = asyncio.create_task(run())
        return {"started": True, "status": "running"}

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for season in self.store.list("seasons"):
            key = _sync_key(season["id"])
            status = self.store.get_meta(key, {})
            if status.get("sync_status") == "running":
                self.store.set_meta(key, {**status, "sync_status": "failed", "errors": ["服务停止，同步中断，可重新同步。"]})
