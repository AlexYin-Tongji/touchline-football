"""Source-backed squad snapshots and team statistics for the personal clubhouse.

Only the fixed ESPN public endpoint is fetched. A squad is replaced atomically
after its team and season have been checked; an upstream failure never wipes a
previous snapshot. Models may explain this data but cannot write its facts.
"""
from __future__ import annotations

import asyncio
import copy
import math
import time
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from . import league_data
from .ingestion import IngestionError, now, published_time, source_error_details
from .player_names import with_reading

ROSTER_CACHE_SECONDS = 86400
RETRY_SECONDS = 60
CORE_PARTS = ("roster", "schedule", "standings")
# ``profile`` is asked for, never picked up on its own. The core three are
# refreshed whenever a page is opened because they are what the page is; a
# club's founding year and nickname are read once and then never change, so
# folding them into the automatic sweep would spend a Wikidata lookup on every
# reader who opens a club to look at the squad list.
#
# ``names`` is optional for the same reason: it costs one round-trip per player
# and returns rows that already have a reading for free, so a squad with
# complete names should not keep paying for the ones it has.
MAINTENANCE_PARTS = (*CORE_PARTS, "portraits", "profile", "names")
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


class _ReadView:
    """One open of the shared collections, reused across a single render.

    A team page used to re-read the whole of ``league_matches`` three times and
    ``league_memberships`` twice, then ask for one team record per fixture and
    per standings row. Every one of those is a full table scan plus a JSON
    decode of the same rows, and they all happen on the read path a 5-second
    poll hammers. This holds the rows for the life of one call instead.

    It is deliberately not a general cache: nothing is stored on the store, and
    the instance dies with the request, so a write that lands mid-render is
    picked up by the next one exactly as before.
    """

    __slots__ = ("_store", "_teams", "_leagues", "_seasons", "_members", "_by_team", "_matches")

    def __init__(self, store):
        self._store = store
        self._teams = self._leagues = self._seasons = None
        # Two indexes over the same collection. They are kept apart rather than
        # sharing one dict because a season id and a team id are both plain
        # strings, and "these never collide in practice" is not a guarantee.
        self._members = {}
        self._by_team = {}
        self._matches = {}

    def team(self, team_id):
        if self._teams is None:
            self._teams = {row["id"]: row for row in self._store.list("teams") if row.get("id")}
        return self._teams.get(team_id)

    def league(self, league_id):
        if self._leagues is None:
            self._leagues = {row["id"]: row for row in self._store.list("leagues") if row.get("id")}
        return self._leagues.get(league_id)

    def seasons_of(self, league_id):
        if self._seasons is None:
            self._seasons = self._store.list("seasons")
        return [row for row in self._seasons if row.get("league_id") == league_id]

    def memberships(self, season_id):
        if season_id not in self._members:
            self._members[season_id] = [row for row in self._store.list("league_memberships")
                                        if row.get("season_id") == season_id]
        return self._members[season_id]

    def team_memberships(self, team_id):
        if team_id not in self._by_team:
            self._by_team[team_id] = [row for row in self._store.list("league_memberships")
                                      if row.get("team_id") == team_id]
        return self._by_team[team_id]

    def matches(self, season_id):
        if season_id not in self._matches:
            self._matches[season_id] = [row for row in self._store.list("league_matches")
                                        if row.get("season_id") == season_id]
        return self._matches[season_id]

    def match_by_id(self, match_id):
        """One fixture by id, from the seasons already open or a direct lookup.

        ``_team_matches`` may be asked for fixtures outside the current season
        (a snapshot recorded a continental tie), and those live in a season
        this view has not opened. That falls back to a single indexed read
        rather than pulling the whole collection in.
        """
        for rows in self._matches.values():
            found = next((row for row in rows if row.get("id") == match_id), None)
            if found:
                return found
        return self._store.get("league_matches", match_id)

    def all_matches(self):
        if self._all_matches is None:
            self._all_matches = self._store.list("league_matches")
        return self._all_matches


def team_season_context(store, team_id, year=None, *, view=None):
    view = view or _ReadView(store)
    team = view.team(team_id)
    if not team:
        raise IngestionError("球队不存在。")
    members = [m for m in view.team_memberships(team_id)
               if year is None or m.get("season_start_year") == year]
    if not members:
        raise IngestionError("这支球队还没有可核对的联赛赛季资料。")
    for member in sorted(members, key=lambda m: m["season_start_year"], reverse=True):
        try:
            league, season, mapping = league_data.season_context(store, member["league_id"], member["season_start_year"])
        except IngestionError:
            if year is not None:
                raise
            continue
        provider_id = str(member.get("provider_team_id", ""))
        if not provider_id.isdigit() or mapping.get(provider_id) != team_id:
            raise IngestionError("球队来源编号与赛季目录不一致。")
        return team, league, season, provider_id
    raise IngestionError("这支球队还没有可核对的完整联赛赛季目录。")


def _snapshot_id(team_id, season_id):
    return f"squad-{season_id}-{team_id}"


def next_kickoff(store, team_id, season_id, *, view=None):
    """The next match still to be played, used to date a squad list properly.

    A squad list changes most in the hours before kickoff, so a flat daily cache
    is at its least appropriate exactly when it matters most.
    """
    view = view or _ReadView(store)
    stamps = []
    for record in view.matches(season_id):
        if team_id not in {record.get("home_team_id"), record.get("away_team_id")}:
            continue
        kickoff = published_time(record.get("kickoff"))
        if kickoff and record.get("status") not in {"finished", "postponed", "cancelled"}:
            stamps.append(kickoff)
    upcoming = sorted(stamp for stamp in stamps if _is_future(stamp))
    return upcoming[0] if upcoming else None


def _is_future(stamp):
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    return parsed > datetime.now(timezone.utc)


def roster_ttl(store, team_id, season_id, *, now=None, view=None):
    """How long a stored squad list stays worth showing without a fresh look.

    Three clocks compose: the transfer window, the twelve hours around kickoff,
    and the season default. The window is the reason this exists — a list that
    is accurate on a Tuesday in October can be three players out of date on
    deadline day, and a single daily constant cannot express that.
    """
    from . import provenance
    kickoff = next_kickoff(store, team_id, season_id, view=view)
    return provenance.ttl_for("roster", kickoff=kickoff, now=now)


def transfer_window_note(store, team_id, season_id, *, now=None, view=None):
    """A short explanation of the current clock, for the reader.

    Without this the interface can only say "updated 4 hours ago", which does
    not tell a reader whether that is recent or overdue for this time of year.
    """
    from . import provenance
    moment = now or datetime.now(timezone.utc)
    kickoff = next_kickoff(store, team_id, season_id, view=view)
    ttl = provenance.ttl_for("roster", kickoff=kickoff, now=moment)
    if ttl <= provenance.MATCH_DAY_TTL:
        return "比赛临近，阵容按两小时刷新。"
    if moment.month in provenance.TRANSFER_WINDOW_MONTHS:
        return "转会窗期间，阵容按半天刷新。"
    return "赛季期间，阵容按天刷新。"


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
    from .team_portraits import espn_headshot
    roster, seen = [], set()
    fetched_at = now()
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
        stats = {}
        for category in ((athlete.get("statistics") or {}).get("splits") or {}).get("categories", []):
            for stat in category.get("stats", []):
                stats[stat.get("name")] = stat.get("value")
        player_stats = {key: _number(stats.get(source), integer=True) for key, source in PLAYER_STATS.items()}
        source_url = f"https://www.espn.com/soccer/player/_/id/{provider_id}"
        # This short introduction contains only roster facts. Tactical opinions,
        # transfer claims and stale coach metadata are deliberately not invented.
        bio_parts = [part for part in (nationality, position_name if group != "OTHER" else "", f"{age}岁" if age is not None else "", f"{number}号" if number else "") if part]
        # The reading is a lookup, never a derivation: a name the table does not
        # hold keeps its source spelling and carries no `name_zh`, so the reader
        # is never shown a Chinese name we guessed.
        row = {"id": "espn-player-" + provider_id, "provider_id": provider_id, "name": name,
               "team_id": team["id"], "season_id": season["id"], "number": number,
               "position": position_name, "position_group": group, "nationality": nationality,
               "birth_date": birth_date, "age": age, "age_as_of": today.isoformat(),
               "height_cm": round(height * 2.54) if height is not None else None,
               "weight_kg": round(weight * .45359237, 1) if weight is not None else None,
               # A source-supplied photo must match this athlete's provider ID;
               # absent or unverified images remain empty for later enrichment.
               "photo_url": "", "photo_credit": "",
               "source_url": source_url, "bio": " · ".join(bio_parts), "stats": player_stats}
        row.update(espn_headshot(athlete, fetched_at=fetched_at))
        if any(record.get(flag) is True for record in (athlete, athlete.get("headshot") or {}) if isinstance(record, dict)
               for flag in ("private", "deleted", "restricted")):
            row["photo_blocked_sources"] = ["espn"]
        # No stored reading is consulted here on purpose: this function is pure
        # parsing, it has no store, and a reading is attached at the display
        # path (``center_view``) where the row's identity is already final.
        roster.append(with_reading(row))
    order = {"GK": 0, "DF": 1, "MF": 2, "FW": 3, "OTHER": 4}
    roster.sort(key=lambda p: (order[p["position_group"]], int(p["number"]) if p["number"] else 999, p["name"]))
    return {"id": _snapshot_id(team["id"], season["id"]), "team_id": team["id"], "season_id": season["id"],
            "league_id": league["id"], "season_start_year": season["season_start_year"], "roster": roster,
            "fetched_at": fetched_at, "source_updated_at": published_time(payload.get("timestamp")),
            "source_url": f"https://www.espn.com/soccer/team/squad/_/id/{provider_team_id}/league/{league['provider_code']}/season/{season['season_start_year']}",
            "provider": "ESPN", "source_method": (payload.get("_touchline_source") or {}).get("method", "api")}


async def sync_roster(store, team_id, year=None, *, force=False):
    team, league, season, provider_id = team_season_context(store, team_id, year)
    snapshot_id = _snapshot_id(team_id, season["id"])
    existing = store.get("team_snapshots", snapshot_id)
    # How long a squad list stays current depends on when it was read, not on
    # when it is being checked, so the budget is derived from its own timestamp.
    ttl = roster_ttl(store, team_id, season["id"], now=existing.get("fetched_at") if existing else None)
    if existing and not force and _elapsed(existing.get("fetched_at")) < ttl:
        return {"cached": True, "updated_at": existing["fetched_at"], "players": len(existing.get("roster", []))}
    url = f"{league_data.API}/apis/site/v2/sports/soccer/{league['provider_code']}/teams/{provider_id}/roster?season={season['season_start_year']}"
    payload = await league_data.fetch_json(url)
    snapshot = parse_roster(payload, team, league, season, provider_id)
    # Core membership and player facts are usable immediately. Optional HTML
    # portrait lookups run separately under TeamMaintenanceManager.
    from .team_portraits import apply_cached_portraits
    snapshot = apply_cached_portraits(store, snapshot)
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


def _team_matches(store, team_id, season_id, sync_status, *, view=None):
    view = view or _ReadView(store)
    ids = sync_status.get("snapshot_match_ids")
    snapshot_ids = set(ids) if isinstance(ids, list) else None
    # This season's fixtures, plus anything a snapshot named by id. The two
    # halves are a union and not an either/or: a named id may sit in another
    # season (a continental tie is stored outside the domestic season), and a
    # fixture in this season that the snapshot did *not* name must stay out —
    # that exclusion is what stops a reduced snapshot from reintroducing a
    # fixture the source has since withdrawn.
    records = [r for r in view.matches(season_id) if team_id in {r.get("home_team_id"), r.get("away_team_id")}]
    if snapshot_ids is not None:
        records = [r for r in records if r["id"] in snapshot_ids]
        seen = {r["id"] for r in records}
        for match_id in snapshot_ids - seen:
            record = view.match_by_id(match_id)
            if record and team_id in {record.get("home_team_id"), record.get("away_team_id")}:
                records.append(record)
    # A club name and a competition name are needed per fixture. Asking the
    # store once per row made a 38-game season 38 separate lookups; the two
    # collections are read once and indexed instead.
    output = []
    for record in sorted(records, key=lambda r: r.get("kickoff") or "9999"):
        home = record["home_team_id"] == team_id
        opponent_id = record["away_team_id"] if home else record["home_team_id"]
        opponent = view.team(opponent_id) or {}
        gf, ga = (record.get("home_score"), record.get("away_score")) if home else (record.get("away_score"), record.get("home_score"))
        gf, ga = _number(gf, integer=True), _number(ga, integer=True)
        if record.get("status") != "finished" or gf is None or ga is None:
            gf, ga = None, None
        competition_id = record.get("league_id") or season_id.rsplit("-", 1)[0]
        output.append({"id": record["id"], "opponent_id": opponent_id, "opponent": opponent.get("name") or (record.get("away_team") if home else record.get("home_team")),
                       "competition_id": competition_id,
                       "competition": record.get("competition") or (view.league(competition_id) or {}).get("name", ""),
                       "opponent_crest_url": opponent.get("crest_url", ""), "venue": "home" if home else "away",
                       "kickoff": record.get("kickoff"), "kickoff_date": record.get("kickoff_date"),
                       "kickoff_confirmed": bool(record.get("kickoff_confirmed")), "status": record.get("status"),
                       "goals_for": gf, "goals_against": ga, "result": ("W" if gf > ga else "L" if gf < ga else "D") if gf is not None and ga is not None else None,
                       "stadium": record.get("stadium", ""), "source_url": record.get("source_url", ""), "updated_at": record.get("fetched_at")})
    return output


def _component_status(updated_at, ttl, available, state=None, *, incomplete=False):
    state = state or {}
    outcome = state.get("status") or state.get("sync_status")
    stale = (not available or _elapsed(updated_at) >= ttl or outcome in {"failed", "interrupted", "unavailable"}
             or outcome == "partial" and bool(state.get("errors")))
    if outcome == "running":
        status = "running"
    elif outcome in {"failed", "interrupted", "unavailable"}:
        status = "failed" if outcome != "unavailable" else outcome
    elif not available:
        status = "missing"
    elif outcome == "partial" or incomplete:
        status = "partial"
    else:
        status = "stale" if stale else "fresh"
    return {"status": status, "updated_at": updated_at, "last_attempt": state.get("last_attempt"),
            "stale": stale, "available": bool(available), "errors": state.get("errors", [])}


def _portrait_retry_seconds(state):
    from .team_portraits import RETRY_SECONDS as PHOTO_RETRY_SECONDS
    # A successfully completed bounded batch has more work to do. That is a
    # continuation, whereas an unavailable source still follows its backoff.
    return 60 if state.get("continuation_ready") is True else PHOTO_RETRY_SECONDS


def _validated_parts(parts):
    if parts is None:
        return None
    if not isinstance(parts, (list, tuple)) or not parts or any(not isinstance(part, str) or part not in MAINTENANCE_PARTS for part in parts):
        raise IngestionError("资料部分只能选择 roster、schedule、standings、portraits、profile 或 names。")
    return tuple(dict.fromkeys(parts))


def _attach_chart_blocks(result):
    """Chart numbers beside the existing charts, without changing that shape."""
    from .team_panel import chart_blocks
    result["chart_blocks"] = chart_blocks(result)
    return result


def center_view(store, team_id, year=None, *, reveal=False):
    view = _ReadView(store)
    team = view.team(team_id)
    if not team:
        raise IngestionError("球队不存在。")
    protected = store.settings().get("spoiler_free", True) and not reveal
    try:
        _, league, season, _ = team_season_context(store, team_id, year, view=view)
    except IngestionError as exc:
        return _attach_chart_blocks({"team": team, "league": None, "season": None, "roster_season": None, "latest_season": None,
                "roster_fallback": False,
                "roster": [], "matches": [],
                "standings": [], "standings_hidden": protected,
                "stats": {"hidden": protected}, "charts": {}, "updated_at": None, "roster_updated_at": None,
                "refresh_status": "unavailable", "errors": [str(exc)], "sources": [], "stale": True,
                "components": {part: _component_status(None, 0, False, {"status": "unavailable", "errors": [str(exc)]}) for part in MAINTENANCE_PARTS},
                "coverage": {"matches": 0, "expected_matches": None, "has_full_schedule": False, "results_complete": None, "basis": "未确认联赛赛季。"}})
    snapshot = store.get("team_snapshots", _snapshot_id(team_id, season["id"])) or {}
    if snapshot and (snapshot.get("team_id") != team_id or snapshot.get("season_id") != season["id"]):
        snapshot = {}
    status = store.get_meta(_status_key(team_id, season["id"]), {})
    league_status = store.get_meta("league_sync_" + season["id"], {})
    recent_status = store.get_meta(league_data.team_schedule_key(season["id"], team_id), {})
    matches = _team_matches(store, team_id, season["id"], recent_status if "snapshot_match_ids" in recent_status else league_status, view=view)
    domestic_matches = [m for m in matches if m["competition_id"] == league["id"]]
    finished = [m for m in domestic_matches if m["status"] == "finished" and m["goals_for"] is not None and m["goals_against"] is not None]
    from .team_portraits import apply_cached_portraits
    visible_snapshot = apply_cached_portraits(store, copy.deepcopy(snapshot))
    roster = [with_reading(row, source_reading=team_names_for(store, row, team_id), team_id=team_id)
              for row in visible_snapshot.get("roster", [])]
    roster_season = ({"id": season["id"], "label": season["label"], "start_year": season["season_start_year"]}
                     if roster else None)
    latest = max(view.seasons_of(league["id"]),
                 key=lambda s: s["season_start_year"], default=season)
    latest_season = {"id": latest["id"], "label": latest["label"], "start_year": latest["season_start_year"]}
    roster_fallback = False
    if year is None and roster and latest["season_start_year"] > season["season_start_year"]:
        try:
            league_data.season_context(store, league["id"], latest["season_start_year"])
        except IngestionError:
            roster_fallback = True
    table = store.get("standings", season["id"]) or {}
    standing = next((r for r in table.get("rows", []) if r.get("team_id") == team_id), None)
    standings = [{**copy.deepcopy(row), "team_name": (view.team(row.get("team_id")) or {}).get("name") or row.get("team_id"),
                  "is_current": row.get("team_id") == team_id} for row in sorted(table.get("rows", []), key=lambda r: r.get("rank", 999))]
    incomplete_results = bool(standing and standing.get("played", 0) > len(finished))
    expected_matches = 2 * (league["expected_team_count"] - 1)
    opponent_ids = {m["team_id"] for m in view.memberships(season["id"]) if m.get("team_id") != team_id}
    required_pairs = {(opponent, venue) for opponent in opponent_ids for venue in ("home", "away")}
    has_full_schedule = len(domestic_matches) == expected_matches and {(m["opponent_id"], m["venue"]) for m in domestic_matches} == required_pairs
    coverage = {"matches": len(domestic_matches), "all_matches": len(matches), "continental_matches": len(matches) - len(domestic_matches),
                "expected_matches": expected_matches, "has_full_schedule": has_full_schedule,
                "results_complete": None if protected or not standing else standing.get("played") == len(finished),
                "basis": "完整性和统计按所属联赛主客场对阵检查；赛程另含来源明确确认参赛的欧冠、欧联、欧协联及欧洲超级杯，不宣称欧战后续轮次已经排定。"}
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
    if matches and league_status.get("fixtures_at"):
        sources.append({"name": "ESPN 联赛赛程", "url": f"https://www.espn.com/soccer/fixtures/_/league/{league['provider_code']}", "updated_at": league_status.get("fixtures_at")})
    results_updated_at = recent_status.get("results_updated_at", recent_status.get("last_sync"))
    if results_updated_at and recent_status.get("source_url"):
        sources.append({"name": "ESPN 球队已赛资料", "url": recent_status["source_url"], "updated_at": results_updated_at})
    if recent_status.get("upcoming_updated_at") and recent_status.get("upcoming_source_url"):
        sources.append({"name": "ESPN 球队未来赛程", "url": recent_status["upcoming_source_url"], "updated_at": recent_status["upcoming_updated_at"]})
    part_sources = {
        "roster": [source for source in sources if "阵容" in source["name"]],
        "schedule": [source for source in sources if "赛程" in source["name"] or "已赛" in source["name"]],
        "standings": [source for source in sources if "积分" in source["name"]],
    }
    part_states = status.get("components", {})
    schedule_times = [recent_status.get(part + "_updated_at") for part in ("results", "upcoming")]
    schedule_time = min(schedule_times) if all(schedule_times) else recent_status.get("last_sync") or league_status.get("fixtures_at")
    schedule_state = recent_status if recent_status else {"sync_status": league_status.get("fixtures_status"), "errors": league_status.get("errors", [])}
    # The manager's running marker is available before its network coroutine
    # starts, but the source adapter remains authoritative about source failure.
    if part_states.get("schedule", {}).get("status") == "running":
        schedule_state = part_states["schedule"]
    standings_state = store.get_meta("league_standings_" + season["id"], {}) or part_states.get("standings", {})
    from .team_portraits import CACHE_SECONDS as PHOTO_CACHE_SECONDS
    portraits_state = part_states.get("portraits", {})
    directory = store.get_meta("team_portraits_v1_" + team_id, {})
    photo_count = sum(bool(player.get("photo_url")) for player in roster)
    if not portraits_state:
        portraits_state = directory
    photo_times = [player.get("photo_updated_at") or snapshot.get("fetched_at") for player in roster if player.get("photo_url")]
    components = {
        "roster": _component_status(snapshot.get("fetched_at"), roster_ttl(store, team_id, season["id"],
                                                                           now=snapshot.get("fetched_at"), view=view),
                                    roster, part_states.get("roster")),
        "schedule": _component_status(schedule_time, league_data.CACHE_SECONDS, matches, schedule_state,
                                      incomplete=not has_full_schedule or (incomplete_results and not protected)),
        "standings": _component_status(table.get("fetched_at"), league_data.CACHE_SECONDS, standing, standings_state),
        "portraits": _component_status(min(photo_times) if photo_times and all(photo_times) else None,
                                      PHOTO_CACHE_SECONDS, photo_count, portraits_state, incomplete=photo_count < len(roster)),
    }
    components["portraits"].update(available_count=photo_count, total_count=len(roster))
    components["portraits"].update(
        remaining_count=portraits_state.get("remaining_count", 0),
        community_remaining_count=portraits_state.get("community_remaining_count", 0),
        community_unavailable=portraits_state.get("community_unavailable", False),
        continuation_ready=portraits_state.get("continuation_ready") is True,
        retry_seconds=_portrait_retry_seconds(portraits_state))
    # The club's own record and its badge. Both are reported from what the record
    # and the released assets already say, which is why neither needs a source
    # check to answer: the question is "is this stated", not "is this true".
    from . import club_profile
    profile_missing = club_profile.missing_fields(team)
    profile_state = part_states.get("profile", {})
    components["profile"] = {
        **_component_status(team.get("profile_updated_at"), club_profile.RECHECK_SECONDS,
                            not profile_missing, profile_state, incomplete=bool(profile_missing)),
        "missing_fields": list(profile_missing),
        "fields": {field: team.get(field) for field in club_profile.FIELDS},
        "source": (team.get("background_sources") or {}).get("wikidata"),
        "crest": club_profile.crest_state(team),
        "note": (profile_state.get("note") or
                 ("球队资料已完整。" if not profile_missing
                  else "缺少：" + "、".join(club_profile.FIELD_LABELS[field] for field in profile_missing))),
    }
    # Why the rest are missing. "17 张没有" reads as a failure; the same gap is
    # ordinary when most of it is players not reached yet, or players a source
    # cannot match at all.
    if photo_count < len(roster):
        from . import photo_acceptance
        from .team_portraits import photo_queue
        # A stored verdict from the last pass is reused rather than recomputed:
        # it carries the comparison against the pass before it, which is the
        # part that says whether work is still moving.
        stored_acceptance = store.get_meta(_photo_acceptance_key(team_id, season["id"]), None)
        acceptance = stored_acceptance if isinstance(stored_acceptance, dict) else None
        if acceptance is None:
            try:
                acceptance = photo_acceptance.assess(roster)
            except Exception:
                acceptance = None
        if acceptance is not None:
            components["portraits"].update(acceptance=acceptance,
                                           acceptance_note=photo_acceptance.summarize(acceptance),
                                           continuation_worthwhile=photo_acceptance.should_continue(
                                               acceptance, attempts=int(acceptance.get("attempts") or 0)))
        # The model's own verdict on the pictures, when a review pass has run.
        # Kept beside the acceptance record, never merged into it: one is a code
        # judgement about coverage, the other a model judgement about the image.
        stored_review = store.get_meta(_photo_review_key(team_id, season["id"]), None)
        if isinstance(stored_review, dict):
            components["portraits"].update(review=stored_review,
                                           review_note=photo_review_note(stored_review))
        try:
            gap = photo_queue(store, snapshot, limit=1)
            components["portraits"].update(untried_count=gap["untried_count"],
                                           settled_count=gap["settled_count"],
                                           no_identity_facts_count=gap["no_identity_facts_count"])
        except Exception:
            pass
    timestamps = [x for x in (snapshot.get("fetched_at"), schedule_time, table.get("fetched_at")) if x]
    errors = list(dict.fromkeys(error for part in CORE_PARTS for error in components[part]["errors"]))
    return _attach_chart_blocks({"team": team, "league": league, "season": {"id": season["id"], "label": season["label"], "start_year": season["season_start_year"]},
            "roster_season": roster_season, "latest_season": latest_season, "roster_fallback": roster_fallback,
            "roster": roster, "roster_count": len(roster), "matches": matches, "stats": stats, "charts": charts,
            "standings": [] if protected else standings, "standings_hidden": protected,
            "updated_at": max(timestamps) if timestamps else None, "roster_updated_at": snapshot.get("fetched_at"),
            "roster_source_updated_at": snapshot.get("source_updated_at"), "fixtures_updated_at": league_status.get("fixtures_at"),
            "team_results_updated_at": results_updated_at,
            "team_schedule_updated_at": recent_status.get("upcoming_updated_at"),
            "standings_updated_at": table.get("fetched_at"), "refresh_status": status.get("status", "idle"),
            "errors": errors, "sources": sources, "part_sources": part_sources,
            "components": components, "coverage": coverage,
            "stale": any(components[part]["stale"] for part in CORE_PARTS)})


def team_context_payload(store, team_id, year=None, *, reveal=False):
    """Compact source-backed facts for Agent tools, not an editable knowledge blob."""
    view = center_view(store, team_id, year, reveal=reveal)
    past = [m for m in view["matches"] if m["status"] == "finished"][-10:]
    upcoming = [m for m in view["matches"] if m["status"] != "finished"][:6]
    return {key: view[key] for key in ("season", "roster_season", "latest_season", "roster_fallback", "roster", "standings", "standings_hidden", "stats", "sources", "updated_at", "roster_updated_at", "refresh_status", "errors", "stale", "components", "coverage")} | {
        "recent_matches": past, "upcoming_matches": upcoming,
        "team_results_updated_at": view.get("team_results_updated_at"),
        # What the club's own record states, so a task can see what is missing
        # and ask for exactly that. Without it the only way to learn whether a
        # founding year was ever filled would be to maintain the record and see.
        "club_profile": {**{field: view.get("team", {}).get(field) for field in ("founded", "stadium", "nickname")},
                         "source": (view.get("team", {}).get("background_sources") or {}).get("wikidata"),
                         "crest": (view.get("components", {}).get("profile") or {}).get("crest"),
                         "component": view.get("components", {}).get("profile")},
        "data_policy": "比分、积分和阵容来自明确赛季的结构化来源。资料更新时间不代表事件发生时间；缺失数据不可补造。球员简介为国籍、位置、年龄、号码字段组合，不含战术评价。球队的创立年份、主场与昵称来自维基数据（CC0），模型只能申请补充，不能自己写入。"}


def _record_component(store, key, part, patch):
    status = store.get_meta(key, {})
    components = status.get("components", {})
    components[part] = {**components.get(part, {}), **patch}
    store.set_meta(key, {**status, "components": components})


def _photo_acceptance_key(team_id, season_id):
    """Where the last photo pass's own judgement is kept for comparison."""
    return f"photo_acceptance_v1_{team_id}_{season_id}"


def _photo_review_key(team_id, season_id):
    """Where a model's usability verdict on the photos is kept.

    Separate from the acceptance record on purpose: acceptance is a code
    judgement about coverage, this is a model judgement about the picture. They
    answer different questions and must be readable apart.
    """
    return f"photo_review_v1_{team_id}_{season_id}"


def photo_review_note(record):
    """One line for the reader, from a stored review record."""
    if not isinstance(record, dict):
        return ""
    retry = record.get("retry_now") or []
    investigate = record.get("investigate") or []
    reviewed = len(record.get("reviews") or [])
    parts = []
    if retry:
        parts.append(f"模型建议立即重试 {len(retry)} 项")
    if investigate:
        parts.append(f"{len(investigate)} 项需进一步核查")
    if not parts:
        return f"模型已复核 {reviewed} 项，均无需动作。" if reviewed else ""
    return "；".join(parts) + f"（本轮共复核 {reviewed} 项）。"


async def review_photo_pass(store, team_id, year=None, *, requester=None, timeout=None):
    """Ask the photo role which sourced portraits are unusable as pictures.

    Optional and opt-in, exactly like the batch planner: a team cycle can run on
    a timer, so an unattended refresh must not start paying for a vision call the
    reader never asked for. Called only from a path that owns a budget.

    The verdict never writes a player field. It is recorded beside the photos so
    the interface and the assistant can report it, and a "needs rework" answer is
    a reason to look again — not permission to delete a photo a source proved.
    """
    from . import photo_review
    _, _, season, _ = team_season_context(store, team_id, year)
    snapshot_id = _snapshot_id(team_id, season["id"])
    snapshot = store.get("team_snapshots", snapshot_id) or {}
    if snapshot.get("team_id") != team_id or snapshot.get("season_id") != season["id"]:
        return None
    roster = snapshot.get("roster") or []
    acceptance = store.get_meta(_photo_acceptance_key(team_id, season["id"]), None)
    if not isinstance(acceptance, dict):
        from . import photo_acceptance
        try:
            acceptance = photo_acceptance.assess(roster)
        except Exception:
            acceptance = None
    kwargs = {} if timeout is None else {"timeout": timeout}
    if requester is not None:
        kwargs["requester"] = requester
    reviews, notes = await photo_review.review_photos(store, roster, acceptance=acceptance, **kwargs)
    # The other three record domains ride the same round: a Chinese name the
    # publishers spell two ways, a squad row the feed shipped without the facts a
    # lookup needs, the club's own missing fields, and a badge whose attribution
    # is incomplete. All four are the same shape of question — code has narrowed
    # it, and what remains is a judgement about what to try next.
    from . import review_records
    record_items = review_records.record_items(roster, store.get("teams", team_id))
    if record_items:
        reviews = list(reviews) + await review_records.review_records(
            store, record_items, **({} if timeout is None else {"timeout": timeout}))
    if not reviews and not notes:
        return None
    record = {"checked_at": now(), "reviews": reviews, "unreadable": notes,
              "retry_now": [item["item_id"] for item in reviews if item["action"] == photo_review.ACTION_RETRY],
              "investigate": [item["item_id"] for item in reviews if item["action"] == photo_review.ACTION_INVESTIGATE]}
    if store.get("team_snapshots", snapshot_id) == snapshot:
        store.set_meta(_photo_review_key(team_id, season["id"]), record)
    return record


async def fetch_player_photos(store, team_id, year=None, *, player_ids=None, force=False, order=None, deadline=None):
    """Share a same-team photo pass across the manual and background callers."""
    _, _, season, _ = team_season_context(store, team_id, year)
    key = _status_key(team_id, season["id"])
    if not hasattr(store, "_team_photo_locks"):
        store._team_photo_locks = {}
        store._team_photo_passes = {}
    lock = store._team_photo_locks.setdefault(key, asyncio.Lock())
    waited = lock.locked()
    signature = (tuple(sorted(str(value) for value in player_ids)) if player_ids is not None else None, bool(force))
    async with lock:
        prior = store._team_photo_passes.get(key)
        snapshot_id = _snapshot_id(team_id, season["id"])
        if (waited and prior and prior["signature"] == signature
                and store.get("team_snapshots", snapshot_id) == prior["snapshot"]):
            return {**prior["result"], "cached": True}
        result = await _fetch_player_photos_once(store, team_id, year, player_ids=player_ids, force=force,
                                                 order=order, deadline=deadline)
        if result.get("status") not in {"failed", "interrupted", "unavailable"}:
            store._team_photo_passes[key] = {"signature": signature, "result": result,
                                             "snapshot": store.get("team_snapshots", snapshot_id)}
        return result


async def _fetch_player_photos_once(store, team_id, year=None, *, player_ids=None, force=False, order=None, deadline=None):
    """Independently fetch attributed photos for the checked roster, never facts."""
    from .team_portraits import enrich_player_photos
    _, _, season, provider_team_id = team_season_context(store, team_id, year)
    snapshot_id = _snapshot_id(team_id, season["id"])
    key = _status_key(team_id, season["id"])
    snapshot = store.get("team_snapshots", snapshot_id) or {}
    if not snapshot.get("roster") or snapshot.get("team_id") != team_id or snapshot.get("season_id") != season["id"]:
        return {"status": "unavailable", "available_count": 0, "total_count": 0, "photos": [],
                "errors": ["请先取得这个球队和赛季的可核对阵容，再补充球员照片。"]}
    _record_component(store, key, "portraits", {"status": "running", "last_attempt": now(), "errors": []})
    # Photos the pass has already checked are written as they land, so a pass
    # cut short by its wall-clock bound keeps what it earned. It used to write
    # once at the end, which meant a timeout discarded every photo fetched in
    # that pass — the reader saw "still missing" and started the same batch
    # over, which is what "it takes several runs" actually was.
    #
    # The guard must answer "has anybody else written since my last save", and
    # it cannot hold a reference to the working copy to do it: that copy is
    # mutated in place, so it changes under the guard's feet. What is held
    # instead is the store's own deep copy of the last body this pass wrote.
    written = {"stored": snapshot}

    def keep(snapshot_in_progress):
        """Persist progress; False means another writer owns the roster now."""
        if store.get("team_snapshots", snapshot_id) != written["stored"]:
            return False
        written["stored"] = store.put("team_snapshots", snapshot_in_progress)
        return True

    try:
        updated, summary = await enrich_player_photos(store, snapshot,
                                                       player_ids=player_ids, force=force,
                                                       order=order, deadline=deadline,
                                                       on_progress=keep)
        # The network wait must never resurrect a replaced roster or alter a
        # freshly corrected membership. Photos attach only to this exact view.
        # `written` carries the store's copy of the last body this pass put
        # down, so the test is "has anyone else written since" rather than
        # "does the store still equal the snapshot we started from" — the
        # latter is false as soon as this pass saves its own progress.
        if store.get("team_snapshots", snapshot_id) != written["stored"]:
            summary = {"status": "interrupted", "errors": ["阵容已在照片获取期间更新，请按新阵容重新核对照片。"]}
        else:
            written["stored"] = store.put("team_snapshots", updated)
        _record_component(store, key, "portraits", summary)
        current = store.get("team_snapshots", snapshot_id) or {}
        # Judge the pass in code, not by asking anything paid. This is the
        # acceptance step the loop was missing: the caller gets a verdict on
        # whether the gaps that remain are reachable, so a repeat pass is a
        # decision with a reason instead of "it still says not fresh".
        from . import photo_acceptance
        acceptance = photo_acceptance.assess(current.get("roster"))
        previous = store.get_meta(_photo_acceptance_key(team_id, season["id"]), None)
        # The attempt count is what makes ``should_continue``'s budget real. It
        # was a parameter no production caller passed, so the only stop that ever
        # fired was "no progress" — which means a pass that gained exactly one
        # photo each time would run for as long as the reader kept opening the
        # team. The count lives beside the last verdict and advances once per
        # pass, which is the unit the budget is written in.
        attempts = int((previous or {}).get("attempts") or 0) + 1 if previous else 1
        summary.update(acceptance=acceptance,
                       acceptance_note=photo_acceptance.summarize(acceptance, previous),
                       continuation_worthwhile=photo_acceptance.should_continue(acceptance, previous,
                                                                               attempts=attempts))
        if store.get("team_snapshots", snapshot_id) == current:
            store.set_meta(_photo_acceptance_key(team_id, season["id"]), {**acceptance, "attempts": attempts})
        _record_component(store, key, "portraits", summary)
        photos = [{k: player.get(k) for k in ("id", "provider_id", "name", "photo_url", "photo_credit", "photo_source_url", "photo_file_page", "photo_license", "photo_restrictions", "photo_updated_at", "photo_match")}
                  for player in current.get("roster", []) if player.get("photo_url")]
        missing = [{k: player.get(k) for k in ("id", "provider_id", "name", "photo_reason", "retryable", "reason_code", "namesake_blocked")}
                   for player in current.get("roster", []) if not player.get("photo_url")]
        return {**summary, "team_id": team_id, "season_id": season["id"], "photos": photos, "missing": missing,
                "available_count": len(photos), "total_count": len(current.get("roster", []))}
    except asyncio.CancelledError:
        _record_component(store, key, "portraits", {"status": "interrupted", "errors": ["照片补充中断，已保存资料保留。"]})
        raise
    except Exception:
        _record_component(store, key, "portraits", {"status": "failed", "errors": ["照片暂未获取完成，已保存资料保留。"]})
        raise


async def sync_portraits(store, team_id, year=None, *, force=False, budget_seconds=None, review=False):
    """Fill optional photos without replacing a newer roster or its timestamp.

    Photo planning is opt-in through ``budget_seconds``: only the caller that
    owns a time budget asks for a model to order the batch. Every other path —
    opening a team, the maintenance queue — runs the ordinary cursor order, so
    a background refresh never spends a model call the reader did not ask for.

    ``review`` adds the vision pass on the same terms: it asks a model to look at
    the photos that came back and say whether they are usable as pictures. It is
    off by default and refused without a budget, because it is a second paid call
    and the same rule has to cover it.
    """
    order, deadline = None, None
    if budget_seconds is not None:
        deadline = time.monotonic() + max(1.0, float(budget_seconds))
        order = await _photo_order(store, team_id, year, deadline=deadline)
    result = await fetch_player_photos(store, team_id, year, force=force, order=order, deadline=deadline)
    if review and budget_seconds is not None:
        result = dict(result)
        try:
            remaining = None if deadline is None else max(5.0, deadline - time.monotonic())
            result["review"] = await review_photo_pass(store, team_id, year, timeout=remaining)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A reviewer that cannot answer leaves the photos exactly as they are.
            result["review"] = None
    return result


def team_names_for(store, row, team_id):
    """The stored Chinese reading for a roster row, or None.

    The read side of the name pass: a stored reading is handed to
    ``with_reading`` so the row carries it, and it goes through the same
    verifier there, so a proof whose ``provider_id`` no longer matches the row
    is dropped rather than displayed. Local only — never a network call, because
    this runs on every roster render.
    """
    from . import team_names
    try:
        return team_names.cached_reading(store, row, team_id)
    except Exception:
        return None


async def sync_names(store, team_id, year=None, *, force=False, deadline=None):
    """Fill in the Chinese reading for roster rows that have none.

    This is the pass that makes a name *available*, separately from the photo
    pass that used to be the only way one could be reached. The two are kept
    apart because they answer different questions against different sources: a
    player can have a citable name and no usable photo, and the reverse. Folding
    names into the photo batch is what left a 27-strong squad with six names —
    the batch advances a photo cursor, so a player whose photo resolved long ago
    was never visited again and his name was never looked up at all.

    Batch-and-cursor like the photo passes, for the same reason: each player
    costs several paced requests, so one run cannot cover a squad and the rest
    has to be carried forward. A player whose reading is already stored and
    still checks out is skipped without a request.
    """
    from . import team_names
    from .team_portraits import PLAYER_PAGE_LIMIT, batch_limit
    _, _, season, _ = team_season_context(store, team_id, year)
    snapshot = store.get("team_snapshots", _snapshot_id(team_id, season["id"])) or {}
    roster = snapshot.get("roster") or []
    if not roster:
        return {"status": "missing", "errors": ["阵容尚未取得，无法核对球员中文名。"],
                "checked": 0, "remaining_count": 0}

    remaining_seconds = None if deadline is None else max(0.0, deadline - time.monotonic())
    limit = batch_limit(remaining_seconds, ceiling=PLAYER_PAGE_LIMIT)
    cursor_key = f"name_cursor_v1_{team_id}_{season['id']}"
    needs = [row for row in roster if force or not team_names.cached_reading(store, row, team_id)]
    if not needs:
        return {"status": "fresh", "checked": 0, "remaining_count": 0,
                "named_count": sum(bool(team_names.cached_reading(store, row, team_id)) for row in roster)}

    offset = int(store.get_meta(cursor_key, {}).get("next", 0)) % len(needs)
    batch = (needs[offset:] + needs[:offset])[:limit]
    errors, checked, named = {}, 0, 0
    for row in batch:
        try:
            record, reason = await team_names.resolve_reading(store, row, team_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A transport fault is not "the source has no reading for him".
            # Kept distinct so the next run retries instead of settling.
            errors[row.get("provider_id") or row.get("name")] = source_error_details(exc)["message"]
            break
        checked += 1
        if record:
            team_names.store_reading(store, row, team_id, record)
            named += 1
        else:
            errors[row.get("provider_id") or row.get("name")] = reason
    store.set_meta(cursor_key, {"next": (offset + len(batch)) % len(needs), "updated_at": now()})
    total_named = sum(bool(team_names.cached_reading(store, row, team_id)) for row in roster)
    return {"status": "completed" if not errors else "partial",
            "checked": checked, "newly_named": named, "named_count": total_named,
            "total_count": len(roster), "remaining_count": max(0, len(roster) - total_named),
            "errors": list(errors.values()), "reasons": errors,
            "updated_at": now()}


async def _photo_order(store, team_id, year, *, deadline=None):
    """The batch order from the photo role, or None to keep the cursor order.

    A planner that cannot answer — no model assigned, a service outage, a
    malformed reply — leaves the refresh running exactly as it does today.
    """
    from . import photo_planning
    from .team_portraits import photo_queue
    try:
        _, _, season, _ = team_season_context(store, team_id, year)
        snapshot = store.get("team_snapshots", _snapshot_id(team_id, season["id"])) or {}
        if not snapshot.get("roster"):
            return None
        report = photo_queue(store, snapshot)
        if not report.get("untried"):
            return None
        seconds = 45.0 if deadline is None else max(5.0, min(45.0, deadline - time.monotonic()))
        return await photo_planning.ordered_batch(store, report, timeout=seconds)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


def _parts_to_refresh(view, requested, force):
    if requested is not None:
        return requested
    missing = tuple(part for part in CORE_PARTS if view["components"][part]["stale"])
    # Retrying a partial refresh reuses its successful, still-current pieces.
    return missing or (CORE_PARTS if force else ())


async def maintain_team(store, team_id, year=None, *, force=False, parts=None, decision_id=None):
    owns_decision = decision_id is None
    requested = _validated_parts(parts)
    _, league, season, _ = team_season_context(store, team_id, year)
    if not hasattr(store, "_team_center_locks"):
        store._team_center_locks = {}
    key = _status_key(team_id, season["id"])
    lock = store._team_center_locks.setdefault(key, asyncio.Lock())
    async with lock:
        prior = store.get_meta(key, {})
        selected = _parts_to_refresh(center_view(store, team_id, year), requested, force)
        selected = tuple(part for part in selected if force and part == "portraits"
                         or _elapsed(prior.get("components", {}).get(part, {}).get("last_attempt")) >= RETRY_SECONDS)
        if not selected:
            return {**prior, "status": "deferred" if decision_id else prior.get("status", "completed"), "cached": True}
        from . import decisions
        if decision_id is None:
            decision_id = decisions.begin(store, team_id, center_view(store, team_id, year, reveal=True),
                                          parts=selected, trigger="manual", reason="按请求更新已过时或缺失的球队资料。")
        started = now()

        async def sync_profile():
            """Fill the club's own record, then persist it only if it changed.

            The write goes through the same ``teams`` record the interface
            edits, so a value written here is visible, editable and reversible
            exactly like one typed by hand.
            """
            from . import club_profile
            team_record = store.get("teams", team_id) or {}
            collected = await club_profile.collect(store, team_record)
            if collected.get("written"):
                updated = collected["team"]
                updated["profile_updated_at"] = now()
                updated["profile_origin"] = "wikidata"
                store.put("teams", updated)
            summary = collected.get("summary") or {}
            return {**summary, "updated_at": now() if collected.get("written") else None}

        store.set_meta(key, {**prior, "status": "running", "started_at": started, "errors": []})
        operations = {
            "roster": lambda: sync_roster(store, team_id, season["season_start_year"], force=force or "roster" in (requested or ())
                                         or prior.get("components", {}).get("roster", {}).get("status") in {"failed", "interrupted"}),
            "schedule": lambda: league_data.sync_team_recent(store, team_id, season["season_start_year"], force=force and prior.get("components", {}).get("schedule", {}).get("status") != "partial"),
            # All teams in a league share one independently cached table.
            "standings": lambda: league_data.sync_standings(store, league["id"], season["season_start_year"], force=force),
            "portraits": lambda: fetch_player_photos(store, team_id, year, force=force),
            # A Chinese reading is checked against the roster row, so it needs
            # the roster to exist; it reads the stored snapshot rather than
            # waiting on a refresh running in the same gather, which is why a
            # squad that is being rebuilt this pass gets its names next pass.
            "names": lambda: sync_names(store, team_id, year, force=force),
            # A club's founding year and nickname are collected, never derived:
            # the model may ask for them and report the outcome, and cannot
            # write them itself.
            "profile": sync_profile,
        }
        async def refresh(part):
            _record_component(store, key, part, {"status": "running", "last_attempt": started, "errors": []})
            try:
                outcome = await operations[part]()
                state = {"status": outcome.get("sync_status", outcome.get("status", "completed")),
                         "errors": outcome.get("errors", []), "updated_at": outcome.get("updated_at") or outcome.get("last_sync"),
                         "cached": bool(outcome.get("cached", False))}
            except asyncio.CancelledError:
                _record_component(store, key, part, {"status": "interrupted", "errors": ["更新中断，已保留上次资料。"]})
                raise
            except Exception as exc:
                state = {"status": "failed", "errors": [str(exc) if isinstance(exc, IngestionError) else "来源暂不可用，已保留上次资料。"]}
            _record_component(store, key, part, state)
        try:
            await asyncio.gather(*(refresh(part) for part in selected))
        except asyncio.CancelledError:
            store.set_meta(key, {**store.get_meta(key, {}), "status": "interrupted", "errors": ["更新中断，上次资料仍可使用。"]})
            if owns_decision:
                decisions.finish(store, decision_id, center_view(store, team_id, year, reveal=True), {"status": "interrupted"})
            raise
        current = store.get_meta(key, {})
        errors = [error for part in selected for error in current.get("components", {}).get(part, {}).get("errors", [])]
        successes = sum(current["components"][part]["status"] not in {"failed", "interrupted", "unavailable"} for part in selected)
        result = {**current, "team_id": team_id, "season_id": season["id"], "status": "completed" if not errors else "partial" if successes else "failed", "errors": errors, "last_attempt": now()}
        if successes:
            result["last_success"] = now()
        store.set_meta(key, result)
        if owns_decision:
            decisions.finish(store, decision_id, center_view(store, team_id, year, reveal=True), result)
        return result


def team_view_claims(store, team_id, view, *, year=None):
    """The centre view as claims, so the freshness planner can read it.

    Each part is claimed with *its own* retrieval time, not the view's. The
    whole page is assembled from several independent snapshots and they age
    separately: a squad list read this morning next to standings read last week
    must not make the standings look current.
    """
    from . import provenance
    subjects = {}
    for part in CORE_PARTS:
        component = (view.get("components") or {}).get(part) or {}
        stamp = component.get("updated_at") or (view.get("updated_at") if part == "standings" else None)
        if part == "roster":
            stamp = view.get("roster_updated_at") or stamp
        records = view.get("matches" if part == "schedule" else part)
        present = bool(records)
        subjects[part] = {"claims": {"roster" if part == "roster" else part: provenance.Claim(
            field="roster" if part == "roster" else part,
            value=len(records or []), source_id="espn", tier=provenance.SourceTier.DATASET,
            fetched_at=stamp or "",
            # The season clock and the fixture clock decide this field's budget;
            # a squad list is the one that also tightens before kickoff.
            ttl_seconds=provenance.ttl_for("roster" if part == "roster" else part,
                                           kickoff=next_kickoff(store, team_id, view.get("season", {}).get("id") or ""),
                                           now=stamp or None),
            note="" if present else "还没有取得这部分资料。")}}
    portraits = (view.get("components") or {}).get("portraits") or {}
    portrait_stamp = portraits.get("last_attempt") or portraits.get("updated_at")
    photo_count = len([player for player in view.get("roster", []) if player.get("photo_url")])
    subjects["portraits"] = {"claims": {"photo_url": provenance.Claim(
        field="photo_url", value=photo_count,
        source_id="portrait_coverage", tier=provenance.SourceTier.DERIVED,
        # A pass that was never attempted is dated now, not left undated. An
        # undated claim ranks first, and "we have not tried yet" is not more
        # urgent than a table that is genuinely six weeks stale — it would let
        # an untouched optional field outrank real staleness on every plan.
        fetched_at=portrait_stamp or view.get("roster_updated_at") or now(),
        ttl_seconds=(int(portraits.get("retry_seconds") or 3600) if photo_count < len(view.get("roster", []))
                     else provenance.YEAR),
        note="" if portrait_stamp else "尚未尝试获取球员照片。")}}
    return subjects


class TeamMaintenanceManager:
    """Deduplicated background maintenance, at most two clubs at a time."""
    def __init__(self, store):
        self.store = store
        self.tasks = {}
        self.portrait_tasks = {}
        self.finalizers = {}
        self.decision_ids = {}
        self.gate = asyncio.Semaphore(2)

    def _round_result(self, key, parts, *, interrupted=False):
        current = self.store.get_meta(key, {})
        components = current.get("components", {})
        errors = [error for part in parts for error in components.get(part, {}).get("errors", [])]
        successes = sum(components.get(part, {}).get("status") not in {"failed", "interrupted", "unavailable"}
                        for part in parts)
        status = ("interrupted" if interrupted else "completed" if not errors
                  else "partial" if successes else "failed")
        result = {**current, "status": status, "errors": errors, "last_attempt": now()}
        if successes:
            result["last_success"] = now()
        self.store.set_meta(key, result)
        return result

    def _finish_after_portraits(self, key, team_id, year, parts, decision_id):
        portrait = self.portrait_tasks[key]
        async def finish():
            from . import decisions
            interrupted = False
            try:
                await portrait
            except asyncio.CancelledError:
                interrupted = True
                if not portrait.done():
                    portrait.cancel()
                    await asyncio.gather(portrait, return_exceptions=True)
                _record_component(self.store, key, "portraits", {"status": "interrupted", "errors": ["照片补充中断，已有资料保留。"]})
            except Exception:
                # The photo adapter has already recorded a bounded failure.
                pass
            result = self._round_result(key, parts, interrupted=interrupted)
            if decision_id:
                decisions.finish(self.store, decision_id, center_view(self.store, team_id, year, reveal=True), result)
            if interrupted and asyncio.current_task().cancelling():
                # The scheduler cancelled this finalizer while awaiting it.
                # Persist the interrupted round first, then let cancellation
                # reach the scheduler instead of turning shutdown into success.
                raise asyncio.CancelledError
            return result
        self.finalizers[key] = asyncio.create_task(finish())

    def _start_portraits(self, team_id, year=None):
        _, _, season, _ = team_season_context(self.store, team_id, year)
        key = _status_key(team_id, season["id"])
        existing = self.portrait_tasks.get(key)
        if existing and not existing.done():
            return False
        view = center_view(self.store, team_id, year)
        portrait_state = self.store.get_meta(key, {}).get("components", {}).get("portraits", {})
        if (not view["roster"] or view["components"]["portraits"]["status"] == "fresh"
                or _elapsed(portrait_state.get("last_attempt")) < _portrait_retry_seconds(portrait_state)):
            return False
        _record_component(self.store, key, "portraits", {"status": "running", "last_attempt": now(), "errors": []})
        self.portrait_tasks[key] = asyncio.create_task(sync_portraits(self.store, team_id, year))
        return True

    def start(self, team_id, year=None, *, force=False, parts=None, decision_id=None):
        requested = _validated_parts(parts)
        _, _, season, _ = team_season_context(self.store, team_id, year)
        key = _status_key(team_id, season["id"])
        task = self.tasks.get(key)
        if task and not task.done():
            return {"started": False, "status": "running", "team_id": team_id}
        finalizer = self.finalizers.get(key)
        if finalizer and not finalizer.done():
            return {"started": False, "status": "running", "team_id": team_id}
        self.tasks.pop(key, None)
        self.finalizers.pop(key, None)
        prior = self.store.get_meta(key, {})
        selected = _parts_to_refresh(center_view(self.store, team_id, year), requested, force)
        selected = tuple(part for part in selected if part in CORE_PARTS
                         and _elapsed(prior.get("components", {}).get(part, {}).get("last_attempt")) >= RETRY_SECONDS)
        with_portraits = requested is None or "portraits" in requested
        if not selected:
            portraits_started = self._start_portraits(team_id, year) if with_portraits else False
            if portraits_started and decision_id:
                self.decision_ids[key] = decision_id
                self._finish_after_portraits(key, team_id, year, ("portraits",), decision_id)
            return {"started": portraits_started, "status": "running" if portraits_started else prior.get("status", "completed"), "team_id": team_id, "cached": not portraits_started}
        self.store.set_meta(key, {**prior, "status": "running", "errors": []})
        async def run():
            from . import decisions
            try:
                async with self.gate:
                    result = await maintain_team(self.store, team_id, year, force=force, parts=selected, decision_id=decision_id)
                if with_portraits:
                    portrait_started = self._start_portraits(team_id, year)
                    portrait = self.portrait_tasks.get(key)
                    if portrait_started or portrait and not portrait.done():
                        self._finish_after_portraits(key, team_id, year, (*selected, "portraits"), decision_id)
                        return result
                    if "portraits" in (requested or ()) and not center_view(self.store, team_id, year)["roster"]:
                        _record_component(self.store, key, "portraits", {"status": "unavailable",
                            "errors": ["暂无可核对阵容，未补充照片。"], "cached": True})
                        result = self._round_result(key, (*selected, "portraits"))
                if decision_id:
                    decisions.finish(self.store, decision_id, center_view(self.store, team_id, year, reveal=True), result)
                return result
            except asyncio.CancelledError:
                status = self.store.get_meta(key, {})
                self.store.set_meta(key, {**status, "status": "interrupted", "errors": ["更新中断，上次资料仍可使用。"]})
                if decision_id:
                    decisions.finish(self.store, decision_id, outcome={"status": "interrupted"})
                raise
            except Exception:
                status = self.store.get_meta(key, {})
                self.store.set_meta(key, {**status, "status": "failed", "last_attempt": now(), "errors": ["更新暂未完成，上次资料仍可使用。"]})
                if decision_id:
                    decisions.finish(self.store, decision_id, outcome={"status": "failed"})
                return {"status": "failed"}
        if decision_id:
            self.decision_ids[key] = decision_id
        self.tasks[key] = asyncio.create_task(run())
        return {"started": True, "status": "running", "team_id": team_id}

    async def review_team(self, team_id, *, budget=12, year=None, now=None, trigger="manual"):
        """Refresh only the parts the freshness planner says are overdue.

        ``start`` refreshes whatever the caller names. This asks what is
        actually out of date and refreshes that, so the same call works for a
        club whose squad changed an hour ago and one whose squad is three weeks
        old — and so a limited request budget goes to the parts that need it
        rather than to whichever part happens to be checked first.
        """
        view = center_view(self.store, team_id, year)
        from . import collection, decisions
        needs = collection.needs_from(team_view_claims(self.store, team_id, view, year=year), now=now)
        plan = collection.plan(needs, budget=budget)
        parts = []
        for need in plan.needs:
            if need.field in CORE_PARTS and need.field not in parts:
                parts.append(need.field)
        # Portraits are only requested when a portrait need is what ranked, so a
        # stale squad list does not also spend the slow photo pass.
        with_portraits = any(need.capability == "image" for need in plan.needs)
        selected = list(parts) + (["portraits"] if with_portraits else [])
        reason, safe_plan = collection.summary(plan), decisions._json_safe(plan.safe())
        identity = decisions.begin(self.store, team_id, center_view(self.store, team_id, year, reveal=True),
                                   parts=selected, trigger=trigger, reason=reason, plan=safe_plan)
        details = {"plan": safe_plan, "reason": reason, "decision_id": identity}
        if not selected:
            decisions.finish(self.store, identity, outcome={"status": "checked" if not needs else "deferred"})
            return {"started": False, "status": "checked" if not needs else "deferred", **details}
        result = self.start(team_id, year, parts=selected, decision_id=identity)
        if not result.get("started"):
            decisions.finish(self.store, identity, outcome={"status": "deferred"})
        return {**result, **details}

    async def wait_for_team(self, team_id, year=None):
        _, _, season, _ = team_season_context(self.store, team_id, year)
        key = _status_key(team_id, season["id"])
        task = self.tasks.get(key)
        portrait = self.portrait_tasks.get(key)
        finalizer = self.finalizers.get(key)
        try:
            if task and not task.cancelled():
                await task
            finalizer = self.finalizers.get(key) or finalizer
            portrait = self.portrait_tasks.get(key) or portrait
            if finalizer:
                await finalizer
            elif portrait:
                await portrait
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                active = [item for item in (task, portrait, finalizer) if item and not item.done()]
                for item in active:
                    item.cancel()
                if active:
                    await asyncio.gather(*active, return_exceptions=True)
                raise
            # A cancelled child belongs to its old or interrupted round, not
            # to the resident scheduler waiting for this team.
        return self.store.get_meta(key, {})

    async def close(self):
        active = [task for task in [*self.tasks.values(), *self.portrait_tasks.values(), *self.finalizers.values()] if not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        from . import decisions
        for identity in self.decision_ids.values():
            decisions.finish(self.store, identity, outcome={"status": "interrupted"})
        # A task cancelled before its coroutine starts never reaches its own
        # handler, so the manager also clears any remaining running markers.
        for key in self.tasks:
            status = self.store.get_meta(key, {})
            if status.get("status") == "running":
                self.store.set_meta(key, {**status, "status": "interrupted", "errors": ["更新中断，上次资料仍可使用。"]})
        for key in self.portrait_tasks:
            status = self.store.get_meta(key, {})
            if status.get("components", {}).get("portraits", {}).get("status") == "running":
                _record_component(self.store, key, "portraits", {"status": "interrupted", "errors": ["照片补充中断，核心资料仍可使用。"]})
