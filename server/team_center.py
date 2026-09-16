"""Source-backed squad snapshots and team statistics for the personal clubhouse.

Only the fixed ESPN public endpoint is fetched. A squad is replaced atomically
after its team and season have been checked; an upstream failure never wipes a
previous snapshot. Models may explain this data but cannot write its facts.
"""
from __future__ import annotations

import asyncio
import copy
import math
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from . import league_data
from .ingestion import IngestionError, now, published_time

ROSTER_CACHE_SECONDS = 86400
RETRY_SECONDS = 60
POSITIONS = {
    "G": ("门将", "GK"), "GK": ("门将", "GK"), "D": ("后卫", "DF"),
    "DF": ("后卫", "DF"), "M": ("中场", "MF"), "MF": ("中场", "MF"),
    "F": ("前锋", "FW"), "FW": ("前锋", "FW"),
}
COUNTRIES = {
    "England": "英格兰", "Scotland": "苏格兰", "Wales": "威尔士", "Northern Ireland": "北爱尔兰",
    "Republic of Ireland": "爱尔兰", "Ireland": "爱尔兰", "Spain": "西班牙", "Portugal": "葡萄牙",
    "France": "法国", "Germany": "德国", "Italy": "意大利", "Netherlands": "荷兰",
    "Belgium": "比利时", "Denmark": "丹麦", "Sweden": "瑞典", "Norway": "挪威",
    "Switzerland": "瑞士", "Austria": "奥地利", "Poland": "波兰", "Czechia": "捷克",
    "Czech Republic": "捷克", "Croatia": "克罗地亚", "Serbia": "塞尔维亚", "Slovenia": "斯洛文尼亚",
    "Slovakia": "斯洛伐克", "Ukraine": "乌克兰", "Turkey": "土耳其", "Türkiye": "土耳其",
    "Greece": "希腊", "Albania": "阿尔巴尼亚", "Romania": "罗马尼亚", "Hungary": "匈牙利",
    "Georgia": "格鲁吉亚", "Iceland": "冰岛", "Finland": "芬兰", "Bosnia and Herzegovina": "波黑",
    "Brazil": "巴西", "Argentina": "阿根廷", "Uruguay": "乌拉圭", "Colombia": "哥伦比亚",
    "Ecuador": "厄瓜多尔", "Paraguay": "巴拉圭", "Chile": "智利", "Venezuela": "委内瑞拉",
    "Peru": "秘鲁", "Mexico": "墨西哥", "United States": "美国", "United States of America": "美国",
    "Canada": "加拿大", "Jamaica": "牙买加", "Costa Rica": "哥斯达黎加", "Panama": "巴拿马",
    "Morocco": "摩洛哥", "Algeria": "阿尔及利亚", "Tunisia": "突尼斯", "Egypt": "埃及",
    "Senegal": "塞内加尔", "Nigeria": "尼日利亚", "Ghana": "加纳", "Cameroon": "喀麦隆",
    "Ivory Coast": "科特迪瓦", "Côte d’Ivoire": "科特迪瓦", "Mali": "马里", "Guinea": "几内亚",
    "DR Congo": "民主刚果", "Congo DR": "民主刚果", "Democratic Republic of the Congo": "民主刚果",
    "Burkina Faso": "布基纳法索", "South Africa": "南非", "Gabon": "加蓬", "Angola": "安哥拉",
    "Japan": "日本", "South Korea": "韩国", "Korea Republic": "韩国", "China": "中国",
    "Australia": "澳大利亚", "New Zealand": "新西兰", "Iran": "伊朗", "Israel": "以色列",
}
PLAYER_STATS = {
    "appearances": "appearances", "goals": "totalGoals", "assists": "goalAssists",
    "shots": "totalShots", "shots_on_target": "shotsOnTarget", "yellow_cards": "yellowCards",
    "red_cards": "redCards", "saves": "saves", "goals_conceded": "goalsConceded",
}


def _elapsed(value):
    parsed = published_time(value)
    return (datetime.now(timezone.utc) - datetime.fromisoformat(parsed)).total_seconds() if parsed else float("inf")


def _number(value, minimum=0, maximum=100000, integer=False):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        if not math.isfinite(number) or not minimum <= number <= maximum or (integer and not number.is_integer()):
            return None
        return int(number) if integer else number
    except (TypeError, ValueError, OverflowError):
        return None


def _text(value, maximum=150):
    return " ".join(value.split())[:maximum] if isinstance(value, str) else ""


def _image_url(value):
    """Only accept an actual image URL supplied by the checked provider record."""
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == "https" and parsed.hostname in {"a.espncdn.com", "a1.espncdn.com", "a2.espncdn.com", "a3.espncdn.com", "a4.espncdn.com"}
                and not parsed.username and not parsed.password and parsed.port in {None, 443}
                and parsed.path.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))):
            return value
    except ValueError:
        pass
    return ""


def team_season_context(store, team_id, year=None):
    team = store.get("teams", team_id)
    if not team:
        raise IngestionError("球队不存在。")
    members = [m for m in store.list("league_memberships") if m.get("team_id") == team_id
               and (year is None or m.get("season_start_year") == year)]
    if not members:
        raise IngestionError("这支球队还没有可核对的联赛赛季资料。")
    member = max(members, key=lambda m: m["season_start_year"])
    league, season, mapping = league_data.season_context(store, member["league_id"], member["season_start_year"])
    provider_id = str(member.get("provider_team_id", ""))
    if not provider_id.isdigit() or mapping.get(provider_id) != team_id:
        raise IngestionError("球队来源编号与赛季目录不一致。")
    return team, league, season, provider_id


def _snapshot_id(team_id, season_id):
    return f"squad-{season_id}-{team_id}"


def _status_key(team_id, season_id):
    return f"team_maintenance_{season_id}_{team_id}"


def parse_roster(payload, team, league, season, provider_team_id):
    """Parse only a positively identified season/team roster, never a guessed one."""
    if not isinstance(payload, dict):
        raise IngestionError("阵容来源返回了无法识别的格式。")
    response_year = (payload.get("season") or {}).get("year")
    if isinstance(response_year, bool) or response_year != season["season_start_year"]:
        raise IngestionError("阵容来源未确认所选赛季，已保留上次阵容。")
    response_team = payload.get("team") or {}
    if str(response_team.get("id")) != str(provider_team_id):
        raise IngestionError("阵容来源的球队编号不匹配，已保留上次阵容。")
    supplied_league = payload.get("league") or {}
    if supplied_league.get("slug") and supplied_league["slug"] != league["provider_code"]:
        raise IngestionError("阵容来源的联赛不匹配。")
    summary = response_team.get("seasonSummary")
    expected_summary = f"{season['season_start_year']}-{str(season['season_start_year'] + 1)[-2:]}"
    if summary and summary not in {expected_summary, season["label"]}:
        raise IngestionError("阵容来源包含冲突的赛季标签。")
    athletes = payload.get("athletes")
    if not isinstance(athletes, list) or not 1 <= len(athletes) <= 120:
        raise IngestionError("来源没有返回完整可读的阵容，已保留上次阵容。")
    roster, seen = [], set()
    today = datetime.now(timezone.utc).date()
    for athlete in athletes:
        if not isinstance(athlete, dict):
            raise IngestionError("阵容记录结构不完整。")
        provider_id = str(athlete.get("id", ""))
        name = _text(athlete.get("displayName") or athlete.get("fullName"))
        if not provider_id.isdigit() or provider_id in seen or not name:
            raise IngestionError("阵容记录缺少唯一球员编号或姓名，未覆盖旧资料。")
        seen.add(provider_id)
        position = athlete.get("position") or {}
        position_name, group = POSITIONS.get(position.get("abbreviation"), ("位置待补充", "OTHER"))
        citizenship = _text(athlete.get("citizenship"), 80)
        nationality = COUNTRIES.get(citizenship, citizenship)
        birth_date, age = "", None
        try:
            birth = date.fromisoformat(str(athlete.get("dateOfBirth", ""))[:10])
            candidate_age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
            if 12 <= candidate_age <= 60:
                birth_date, age = birth.isoformat(), candidate_age
        except ValueError:
            pass
        if age is None:
            age = _number(athlete.get("age"), 12, 60, integer=True)
        height = _number(athlete.get("height"), 48, 90)
        weight = _number(athlete.get("weight"), 75, 300)
        number = _text(str(athlete.get("jersey") or ""), 3)
        number = number if number.isdigit() else ""
        headshot = athlete.get("headshot") or {}
        photo = _image_url(headshot.get("href")) if isinstance(headshot, dict) else ""
        stats = {}
        for category in ((athlete.get("statistics") or {}).get("splits") or {}).get("categories", []):
            for stat in category.get("stats", []):
                stats[stat.get("name")] = stat.get("value")
        player_stats = {key: _number(stats.get(source), integer=True) for key, source in PLAYER_STATS.items()}
        source_url = f"https://www.espn.com/soccer/player/_/id/{provider_id}"
        # This short introduction contains only roster facts. Tactical opinions,
        # transfer claims and stale coach metadata are deliberately not invented.
        bio_parts = [part for part in (nationality, position_name if group != "OTHER" else "", f"{age}岁" if age is not None else "", f"{number}号" if number else "") if part]
        roster.append({"id": "espn-player-" + provider_id, "provider_id": provider_id, "name": name,
                       "team_id": team["id"], "season_id": season["id"], "number": number,
                       "position": position_name, "position_group": group, "nationality": nationality,
                       "birth_date": birth_date, "age": age, "age_as_of": today.isoformat(),
                       "height_cm": round(height * 2.54) if height is not None else None,
                       "weight_kg": round(weight * .45359237, 1) if weight is not None else None,
                       "photo_url": photo, "photo_credit": "ESPN 球员资料图片" if photo else "",
                       "source_url": source_url, "bio": " · ".join(bio_parts), "stats": player_stats})
    order = {"GK": 0, "DF": 1, "MF": 2, "FW": 3, "OTHER": 4}
    roster.sort(key=lambda p: (order[p["position_group"]], int(p["number"]) if p["number"] else 999, p["name"]))
    return {"id": _snapshot_id(team["id"], season["id"]), "team_id": team["id"], "season_id": season["id"],
            "league_id": league["id"], "season_start_year": season["season_start_year"], "roster": roster,
            "fetched_at": now(), "source_updated_at": published_time(payload.get("timestamp")),
            "source_url": f"https://www.espn.com/soccer/team/squad/_/id/{provider_team_id}/league/{league['provider_code']}/season/{season['season_start_year']}",
            "provider": "ESPN"}


async def sync_roster(store, team_id, year=None, *, force=False):
    team, league, season, provider_id = team_season_context(store, team_id, year)
    snapshot_id = _snapshot_id(team_id, season["id"])
    existing = store.get("team_snapshots", snapshot_id)
    if existing and not force and _elapsed(existing.get("fetched_at")) < ROSTER_CACHE_SECONDS:
        return {"cached": True, "updated_at": existing["fetched_at"], "players": len(existing.get("roster", []))}
    url = f"{league_data.API}/apis/site/v2/sports/soccer/{league['provider_code']}/teams/{provider_id}/roster?season={season['season_start_year']}"
    payload = await league_data.fetch_json(url)
    snapshot = parse_roster(payload, team, league, season, provider_id)
    from .team_portraits import enrich_portraits
    snapshot = await enrich_portraits(store, snapshot)
    store.put("team_snapshots", snapshot)
    return {"cached": False, "updated_at": snapshot["fetched_at"], "players": len(snapshot["roster"])}


def _tally(matches):
    result = {"played": len(matches), "won": 0, "drawn": 0, "lost": 0, "goals_for": 0, "goals_against": 0, "points": 0, "win_rate": None}
    for match in matches:
        gf, ga = match["goals_for"], match["goals_against"]
        result["goals_for"] += gf
        result["goals_against"] += ga
        result["won" if gf > ga else "lost" if gf < ga else "drawn"] += 1
    result["points"] = result["won"] * 3 + result["drawn"]
    result["goal_difference"] = result["goals_for"] - result["goals_against"]
    result["win_rate"] = round(100 * result["won"] / len(matches), 1) if matches else None
    return result


def _team_matches(store, team_id, season_id, sync_status):
    ids = sync_status.get("snapshot_match_ids")
    snapshot_ids = set(ids) if isinstance(ids, list) else None
    records = [r for r in store.list("league_matches") if r.get("season_id") == season_id
               and team_id in {r.get("home_team_id"), r.get("away_team_id")}
               and (snapshot_ids is None or r["id"] in snapshot_ids)]
    output = []
    for record in sorted(records, key=lambda r: r.get("kickoff") or "9999"):
        home = record["home_team_id"] == team_id
        opponent_id = record["away_team_id"] if home else record["home_team_id"]
        opponent = store.get("teams", opponent_id) or {}
        gf, ga = (record.get("home_score"), record.get("away_score")) if home else (record.get("away_score"), record.get("home_score"))
        gf, ga = _number(gf, integer=True), _number(ga, integer=True)
        if record.get("status") != "finished" or gf is None or ga is None:
            gf, ga = None, None
        output.append({"id": record["id"], "opponent_id": opponent_id, "opponent": opponent.get("name") or (record.get("away_team") if home else record.get("home_team")),
                       "opponent_crest_url": opponent.get("crest_url", ""), "venue": "home" if home else "away",
                       "kickoff": record.get("kickoff"), "kickoff_date": record.get("kickoff_date"),
                       "kickoff_confirmed": bool(record.get("kickoff_confirmed")), "status": record.get("status"),
                       "goals_for": gf, "goals_against": ga, "result": ("W" if gf > ga else "L" if gf < ga else "D") if gf is not None and ga is not None else None,
                       "stadium": record.get("stadium", ""), "source_url": record.get("source_url", ""), "updated_at": record.get("fetched_at")})
    return output


def center_view(store, team_id, year=None, *, reveal=False):
    team = store.get("teams", team_id)
    if not team:
        raise IngestionError("球队不存在。")
    protected = store.settings().get("spoiler_free", True) and not reveal
    try:
        _, league, season, _ = team_season_context(store, team_id, year)
    except IngestionError as exc:
        return {"team": team, "league": None, "season": None, "roster": [], "matches": [],
                "stats": {"hidden": protected}, "charts": {}, "updated_at": None, "roster_updated_at": None,
                "refresh_status": "unavailable", "errors": [str(exc)], "sources": [], "stale": True}
    snapshot = store.get("team_snapshots", _snapshot_id(team_id, season["id"])) or {}
    if snapshot and (snapshot.get("team_id") != team_id or snapshot.get("season_id") != season["id"]):
        snapshot = {}
    status = store.get_meta(_status_key(team_id, season["id"]), {})
    league_status = store.get_meta("league_sync_" + season["id"], {})
    recent_status = store.get_meta(league_data.team_schedule_key(season["id"], team_id), {})
    matches = _team_matches(store, team_id, season["id"], league_status)
    finished = [m for m in matches if m["status"] == "finished" and m["goals_for"] is not None and m["goals_against"] is not None]
    roster = copy.deepcopy(snapshot.get("roster", []))
    table = store.get("standings", season["id"]) or {}
    standing = next((r for r in table.get("rows", []) if r.get("team_id") == team_id), None)
    incomplete_results = bool(standing and standing.get("played", 0) > len(finished))
    stats = {"hidden": protected, "season": _tally(finished),
             "home": _tally([m for m in finished if m["venue"] == "home"]), "away": _tally([m for m in finished if m["venue"] == "away"]),
             "recent": [m["result"] for m in finished[-5:]], "standing": standing,
             "sample_complete": standing is not None and standing.get("played") == len(finished),
             "basis": "本赛季已收录且比分完整的联赛比赛；积分榜使用来源发布值（可能含扣分）。"}
    charts = {"goals_by_match": [{"id": m["id"], "label": m["opponent"], "date": m["kickoff_date"], "goals_for": m["goals_for"], "goals_against": m["goals_against"], "source_url": m["source_url"]} for m in finished[-10:]],
              "results": [{"label": label, "value": stats["season"][key]} for label, key in (("胜", "won"), ("平", "drawn"), ("负", "lost"))],
              "home_away": [{"label": "主场", **stats["home"]}, {"label": "客场", **stats["away"]}]}
    if protected:
        for match in matches:
            match.update(goals_for=None, goals_against=None, result=None, spoiler_hidden=True)
        for player in roster:
            player["stats"] = {}
        stats, charts = {"hidden": True}, {}
    sources = []
    if snapshot.get("source_url"):
        sources.append({"name": "ESPN 球队阵容", "url": snapshot["source_url"], "updated_at": snapshot.get("fetched_at")})
    if table.get("source_url"):
        sources.append({"name": "ESPN 联赛积分", "url": table["source_url"], "updated_at": table.get("fetched_at")})
    if matches:
        sources.append({"name": "ESPN 联赛赛程", "url": f"https://www.espn.com/soccer/fixtures/_/league/{league['provider_code']}", "updated_at": league_status.get("fixtures_at")})
    results_updated_at = recent_status.get("results_updated_at", recent_status.get("last_sync"))
    if results_updated_at and recent_status.get("source_url"):
        sources.append({"name": "ESPN 球队已赛资料", "url": recent_status["source_url"], "updated_at": results_updated_at})
    if recent_status.get("upcoming_updated_at") and recent_status.get("upcoming_source_url"):
        sources.append({"name": "ESPN 球队未来赛程", "url": recent_status["upcoming_source_url"], "updated_at": recent_status["upcoming_updated_at"]})
    timestamps = [x for x in (snapshot.get("fetched_at"), league_status.get("fixtures_at"), table.get("fetched_at"), recent_status.get("last_sync")) if x]
    needs_recent = incomplete_results or league_status.get("fixtures_status") == "failed" or any(str(error).startswith("赛程") for error in league_status.get("errors", []))
    return {"team": team, "league": league, "season": {"id": season["id"], "label": season["label"], "start_year": season["season_start_year"]},
            "roster": roster, "roster_count": len(roster), "matches": matches, "stats": stats, "charts": charts,
            "updated_at": max(timestamps) if timestamps else None, "roster_updated_at": snapshot.get("fetched_at"),
            "roster_source_updated_at": snapshot.get("source_updated_at"), "fixtures_updated_at": league_status.get("fixtures_at"),
            "team_results_updated_at": results_updated_at,
            "team_schedule_updated_at": recent_status.get("upcoming_updated_at"),
            "standings_updated_at": table.get("fetched_at"), "refresh_status": status.get("status", "idle"),
            "errors": list(dict.fromkeys(status.get("errors", []) + league_status.get("errors", []) + recent_status.get("errors", []))), "sources": sources,
            "stale": (_elapsed(snapshot.get("fetched_at")) >= ROSTER_CACHE_SECONDS or _elapsed(league_status.get("last_sync")) >= league_data.CACHE_SECONDS
                      or needs_recent and _elapsed(recent_status.get("last_attempt")) >= league_data.CACHE_SECONDS)}


def team_context_payload(store, team_id, year=None, *, reveal=False):
    """Compact source-backed facts for Agent tools, not an editable knowledge blob."""
    view = center_view(store, team_id, year, reveal=reveal)
    past = [m for m in view["matches"] if m["status"] == "finished"][-10:]
    upcoming = [m for m in view["matches"] if m["status"] != "finished"][:6]
    return {key: view[key] for key in ("season", "roster", "stats", "sources", "updated_at", "roster_updated_at", "refresh_status", "errors", "stale")} | {
        "recent_matches": past, "upcoming_matches": upcoming,
        "team_results_updated_at": view.get("team_results_updated_at"),
        "data_policy": "比分、积分和阵容来自明确赛季的结构化来源。资料更新时间不代表事件发生时间；缺失数据不可补造。球员简介为国籍、位置、年龄、号码字段组合，不含战术评价。"}


async def maintain_team(store, team_id, year=None, *, force=False):
    _, league, season, _ = team_season_context(store, team_id, year)
    if not hasattr(store, "_team_center_locks"):
        store._team_center_locks = {}
    key = _status_key(team_id, season["id"])
    lock = store._team_center_locks.setdefault(key, asyncio.Lock())
    async with lock:
        prior = store.get_meta(key, {})
        if _elapsed(prior.get("last_attempt")) < RETRY_SECONDS:
            return {**prior, "cached": True}
        if not force and not center_view(store, team_id, year)["stale"]:
            return {**prior, "status": prior.get("status", "completed"), "cached": True}
        started = now()
        store.set_meta(key, {**prior, "status": "running", "started_at": started, "errors": []})
        try:
            results = await asyncio.gather(
                league_data.sync_league(store, league["id"], season["season_start_year"], force=False),
                sync_roster(store, team_id, season["season_start_year"], force=force), return_exceptions=True)
        except asyncio.CancelledError:
            store.set_meta(key, {**prior, "status": "interrupted", "errors": ["更新中断，上次资料仍可使用。"]})
            raise
        labels = ["赛程与积分", "球队阵容"]
        league_result = results[0]
        current = center_view(store, team_id, year, reveal=True)
        standing = current["stats"].get("standing")
        missing_results = bool(standing and standing.get("played", 0) > current["stats"].get("season", {}).get("played", 0))
        fixtures_failed = (isinstance(league_result, BaseException) or league_result.get("fixtures_status") == "failed"
                           or any(str(error).startswith("赛程") for error in league_result.get("errors", []))
                           or league_result.get("sync_status") == "failed")
        if fixtures_failed or missing_results:
            try:
                supplemental = await league_data.sync_team_recent(store, team_id, season["season_start_year"], force=force)
            except asyncio.CancelledError:
                store.set_meta(key, {**prior, "status": "interrupted", "errors": ["更新中断，上次资料仍可使用。"]})
                raise
            except Exception as exc:
                supplemental = exc
            results.append(supplemental)
            labels.append("球队近期赛程")
        errors, successes = [], 0
        for label, result in zip(labels, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                errors.append(f"{label}：" + (str(result) if isinstance(result, IngestionError) else "来源暂不可用，已保留上次资料。"))
            else:
                errors.extend(result.get("errors", []))
                if result.get("sync_status") != "failed":
                    successes += 1
        result = {**prior, "team_id": team_id, "season_id": season["id"], "status": "completed" if not errors else "partial" if successes else "failed", "errors": errors, "last_attempt": now()}
        if successes:
            result["last_success"] = now()
        store.set_meta(key, result)
        return result


class TeamMaintenanceManager:
    """Deduplicated background maintenance, at most two clubs at a time."""
    def __init__(self, store):
        self.store = store
        self.tasks = {}
        self.gate = asyncio.Semaphore(2)

    def start(self, team_id, year=None, *, force=False):
        _, _, season, _ = team_season_context(self.store, team_id, year)
        key = _status_key(team_id, season["id"])
        task = self.tasks.get(key)
        if task and not task.done():
            return {"started": False, "status": "running", "team_id": team_id}
        prior = self.store.get_meta(key, {})
        if _elapsed(prior.get("last_attempt")) < RETRY_SECONDS or (not force and not center_view(self.store, team_id, year)["stale"]):
            return {"started": False, "status": prior.get("status", "completed"), "team_id": team_id, "cached": True}
        self.store.set_meta(key, {**prior, "status": "running", "errors": []})
        async def run():
            try:
                async with self.gate:
                    await maintain_team(self.store, team_id, year, force=force)
            except asyncio.CancelledError:
                status = self.store.get_meta(key, {})
                self.store.set_meta(key, {**status, "status": "interrupted", "errors": ["更新中断，上次资料仍可使用。"]})
                raise
            except Exception:
                status = self.store.get_meta(key, {})
                self.store.set_meta(key, {**status, "status": "failed", "last_attempt": now(), "errors": ["更新暂未完成，上次资料仍可使用。"]})
        self.tasks[key] = asyncio.create_task(run())
        return {"started": True, "status": "running", "team_id": team_id}

    async def close(self):
        active = [task for task in self.tasks.values() if not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        # A task cancelled before its coroutine starts never reaches its own
        # handler, so the manager also clears any remaining running markers.
        for key in self.tasks:
            status = self.store.get_meta(key, {})
            if status.get("status") == "running":
                self.store.set_meta(key, {**status, "status": "interrupted", "errors": ["更新中断，上次资料仍可使用。"]})
