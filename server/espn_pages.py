"""Narrow fallback to ESPN's normal public pages when its public API denies a request.

Only known league/team/season routes are constructed. We decode the page's JSON
state, never execute JavaScript or follow embedded URLs. Existing domain parsers
still validate membership, seasons, full standings and score arithmetic.
"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx

from .ingestion import IngestionError, now
from .league_catalog import LEAGUES, EUROPEAN_COMPETITIONS

MAX_BODY = 4 * 1024 * 1024
LEAGUE_UIDS = {row["provider_code"]: row["provider_league_uid"] for row in LEAGUES}
PLAYER_STATS = ("appearances", "totalGoals", "goalAssists", "totalShots", "shotsOnTarget",
                "yellowCards", "redCards", "saves", "goalsConceded")
STANDING_FIELDS = {"rank": "rank", "gamesplayed": "gamesPlayed", "wins": "wins", "ties": "ties", "losses": "losses",
                   "pointsfor": "pointsFor", "pointsagainst": "pointsAgainst", "pointdifferential": "pointDifferential", "points": "points"}


def fallback_spec(api_url):
    """Return a fixed public route only for the three supported API shapes."""
    try:
        parsed = urlsplit(api_url)
        if (parsed.scheme != "https" or parsed.hostname != "site.api.espn.com" or parsed.port not in {None, 443}
                or parsed.username or parsed.password or parsed.fragment):
            return None
        team = re.fullmatch(r"/apis/site/v2/sports/soccer/([a-z._0-9]+)/teams/([0-9]{1,10})/(roster|schedule)", parsed.path)
        table = re.fullmatch(r"/apis/v2/sports/soccer/([a-z]+\.1)/standings", parsed.path)
        if not team and not table:
            return None
        league = (team or table).group(1)
        query = parse_qs(parsed.query, keep_blank_values=True)
        allowed = {"season", "fixture"} if team and team.group(3) == "schedule" else {"season"}
        allowed_leagues = {*LEAGUE_UIDS, "all", *EUROPEAN_COMPETITIONS} if team and team.group(3) == "schedule" else set(LEAGUE_UIDS)
        if league not in allowed_leagues or not set(query).issubset(allowed) or any(len(value) != 1 for value in query.values()):
            return None
        year = query.get("season", [""])[0]
        if not re.fullmatch(r"20[0-9]{2}", year) or query.get("fixture", ["true"])[0] != "true":
            return None
        kind = "standings" if table else "squad" if team.group(3) == "roster" else "fixtures" if "fixture" in query else "results"
        team_id = team.group(2) if team else None
        path = f"team/{kind}/_/id/{team_id}/league/{league}" if team else f"standings/_/league/{league}"
        return {"kind": kind, "league": league, "year": int(year), "team_id": team_id,
                "url": "https://www.espn.com/soccer/" + path + "/season/" + year}
    except (TypeError, ValueError):
        return None


def _fail(message):
    raise IngestionError("ESPN 公开网页" + message + "，已保留上次资料。")


def _object(value):
    if not isinstance(value, dict):
        _fail("结构不完整")
    return value


def _array(value, maximum=250, *, allow_empty=False):
    if not isinstance(value, list) or len(value) > maximum or not value and not allow_empty:
        _fail("缺少可核对的数据列表")
    return value


def _number(value, *, required=False):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]{1,8}", value.strip()):
        return int(value)
    if required:
        _fail("缺少明确的数值，不将占位符当作零")
    return None


def _year(value, expected):
    if type(value) is not int or value != expected:
        _fail("返回了其他赛季或没有确认赛季")


def _team(content, spec):
    team = _object(content.get("team"))
    if str(team.get("id")) != spec["team_id"]:
        _fail("的球队编号不匹配")
    return team


def _roster(content, spec):
    metadata = _object(content.get("metadata"))
    _year(metadata.get("year"), spec["year"])
    label = f"{spec['year']}-{str(spec['year'] + 1)[-2:]}"
    if metadata.get("season") != label:
        _fail("的阵容赛季标签不匹配")
    matching = [item for item in _array(content.get("leaguesDropdown"), 200)
                if isinstance(item, dict) and str(item.get("value", "")).lower() == spec["league"]]
    if len(matching) != 1 or not matching[0].get("label") or metadata.get("league") != matching[0]["label"]:
        _fail("的阵容联赛不匹配")
    team = _team(content, spec)
    athletes, seen = [], set()
    for group in _array(content.get("groups"), 12):
        for player in _array(_object(group).get("athletes"), 120, allow_empty=True):
            player = _object(player)
            uid = re.fullmatch(r"s:600~a:([0-9]+)", str(player.get("uid", "")))
            link = urlsplit(str(player.get("href", "")))
            linked_id = re.match(r"/soccer/player/_/id/([0-9]+)(?:/|$)", link.path)
            if (not uid or not linked_id or uid.group(1) != linked_id.group(1) or uid.group(1) in seen
                    or link.scheme != "https" or link.hostname != "www.espn.com" or link.username or link.password
                    or link.port not in {None, 443}):
                _fail("的球员编号或来源链接不一致")
            identity = uid.group(1)
            seen.add(identity)
            height = re.fullmatch(r"([4-7])'\s*([0-9]{1,2})\"", str(player.get("height", "")))
            weight = re.fullmatch(r"([0-9]{2,3}) lbs", str(player.get("weight", "")))
            stats = [{"name": field, "value": value} for field in PLAYER_STATS if (value := _number(player.get(field))) is not None]
            athletes.append({"id": identity, "displayName": player.get("name"), "jersey": player.get("jersey"),
                "citizenship": player.get("ctz"), "age": _number(player.get("age")),
                "position": {"abbreviation": player.get("position")},
                "height": int(height.group(1)) * 12 + int(height.group(2)) if height and int(height.group(2)) < 12 else None,
                "weight": int(weight.group(1)) if weight else None,
                "headshot": player.get("headshot") if isinstance(player.get("headshot"), dict) else {},
                "statistics": {"splits": {"categories": [{"stats": stats}]}}})
    _array(athletes, 120)
    return {"season": {"year": metadata["year"]}, "team": {"id": team["id"], "seasonSummary": label},
            "league": {"slug": spec["league"]}, "athletes": athletes}


def _schedule(content, spec):
    season = _object(content.get("season"))
    _year(season.get("year"), spec["year"])
    if content.get("seasonTypeYear") != f"{spec['year']}-{str(spec['year'] + 1)[-2:]}":
        _fail("的赛程赛季标签不匹配")
    team = _team(content, spec)
    leagues = _array(content.get("dropdownLeagues"), 200)
    matching = [item for item in leagues if isinstance(item, dict) and str(item.get("value", "")).lower() == spec["league"]]
    if len(matching) != 1 or not matching[0].get("title") or matching[0]["title"] != content.get("competitionName"):
        _fail("未明确确认所选联赛")
    league_name = matching[0]["title"]
    names = {}
    for item in leagues:
        if isinstance(item, dict) and item.get("title") and item.get("value"):
            names.setdefault(item["title"], set()).add(str(item["value"]).lower())
    events = []
    for event in _array(content.get("events")):
        event = _object(event)
        codes = names.get(event.get("league"), set())
        if len(codes) != 1:
            _fail("的赛事名称没有唯一来源编号")
        event_code = next(iter(codes))
        if spec["league"] != "all" and event.get("league") != league_name:
            _fail("混入了其他赛事")
        if event_code not in LEAGUE_UIDS and event_code not in EUROPEAN_COMPETITIONS:
            continue
        competitors = _array(event.get("competitors"), 2)
        if (len(competitors) != 2 or any(not isinstance(row, dict) or type(row.get("isHome")) is not bool for row in competitors)
                or {row["isHome"] for row in competitors} != {True, False}
                or spec["team_id"] not in {str(row.get("id")) for row in competitors}):
            _fail("的比赛球队或主客场无法核对")
        state = _object(event.get("status"))
        finished = event.get("completed") is True and state.get("state") == "post"
        scores = None
        if finished:
            if competitors[0]["isHome"] is not True or competitors[1]["isHome"] is not False:
                _fail("的比分主客顺序不明确")
            # On these public pages competitors[].score can be placeholder 0
            # even after FT. Only the explicit displayed score is authoritative.
            score = re.fullmatch(r"\s*([0-9]{1,3})\s*[-–]\s*([0-9]{1,3})\s*", str(event.get("score", "")))
            if not score:
                _fail("的已赛记录没有明确完整比分")
            scores = [int(value) for value in score.groups()]
            for index, row in enumerate(competitors):
                if row.get("winner") is True and scores[index] <= scores[1 - index]:
                    _fail("的比分与胜者标记冲突")
            label = "STATUS_FULL_TIME"
        elif event.get("completed") is False and state.get("state") in {"pre", "in"}:
            detail = str(state.get("detail", "")).upper()
            label = ("STATUS_POSTPONED" if any(word in detail for word in ("POSTPON", "SUSPEND", "ABANDON")) else
                     "STATUS_CANCELED" if "CANCEL" in detail else "STATUS_IN_PROGRESS" if state["state"] == "in" else "STATUS_SCHEDULED")
        else:
            _fail("的比赛完成状态不明确")
        typ = {"name": label, "state": state["state"], "completed": finished}
        players = [{"id": row.get("id"), "type": "team", "homeAway": "home" if row["isHome"] else "away",
                    "team": {"displayName": row.get("displayName", "")}, "score": scores[index] if scores else None}
                   for index, row in enumerate(competitors)]
        events.append({"id": event.get("id"), "date": event.get("date"), "season": {"year": season["year"]},
                       "league": {"slug": event_code, **({"id": LEAGUE_UIDS[event_code].rsplit(":", 1)[-1]} if event_code in LEAGUE_UIDS else {})},
                       "competitions": [{"date": event.get("date"), "timeValid": event.get("tbd") is False,
                                         "status": {"type": typ}, "competitors": players,
                                         "venue": event.get("venue") if isinstance(event.get("venue"), dict) else {}}]})
    return {"season": {"year": season["year"]}, "requestedSeason": {"year": season["year"]},
            "team": {"id": team["id"], "seasonSummary": content["seasonTypeYear"]}, "events": events}


def _standings(content, spec):
    _year(_object(content.get("currentSeason")).get("year"), spec["year"])
    _year(_object(content.get("requestedDates")).get("season"), spec["year"])
    if content.get("leagueUid") != LEAGUE_UIDS[spec["league"]]:
        _fail("的积分联赛编号不匹配")
    groups = _object(content.get("groups"))
    headers = _object(groups.get("headers"))
    columns = {}
    for name, canonical in STANDING_FIELDS.items():
        header = _object(headers.get(name))
        if header.get("t") != name or type(header.get("i")) is not int or not 0 <= header["i"] < 100:
            _fail("的积分列定义不完整")
        columns[canonical] = header["i"]
    if len(set(columns.values())) != len(columns):
        _fail("的积分列重复")
    entries = []
    for group in _array(groups.get("groups"), 10):
        for row in _array(_object(group).get("standings"), 40):
            row = _object(row)
            team = _object(row.get("team"))
            identity = str(team.get("id", ""))
            if not identity.isdigit() or team.get("uid") != "s:600~t:" + identity:
                _fail("的积分球队编号不一致")
            values = _array(row.get("stats"), 100)
            if max(columns.values()) >= len(values):
                _fail("的积分数值不完整")
            entries.append({"team": {"id": identity}, "stats": [{"name": field, "value": _number(values[index], required=True)} for field, index in columns.items()]})
    return {"season": {"year": spec["year"]}, "uid": content["leagueUid"], "children": [{"standings": {"entries": entries}}]}


def parse_page(body, spec):
    if not spec:
        _fail("不支持这个请求范围")
    if isinstance(body, bytes):
        if len(body) > MAX_BODY:
            _fail("超过读取上限")
        try:
            body = body.decode("utf-8")
        except UnicodeError:
            _fail("编码无法识别")
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_BODY:
        _fail("超过读取上限")
    markers = list(re.finditer(r"window(?:\[['\"]__espnfitt__['\"]\]|\.__espnfitt__)\s*=\s*", body))
    if len(markers) != 1:
        _fail("缺少唯一的结构化资料")
    try:
        payload, _ = json.JSONDecoder().raw_decode(body[markers[0].end():])
    except (ValueError, RecursionError):
        _fail("结构化资料无法读取")
    params = _object(_object(_object(payload).get("routing")).get("params"))
    page = _object(payload.get("page"))
    kind = spec["kind"]
    if (params.get("sport") != "soccer" or str(params.get("league", "")).lower() != spec["league"]
            or str(params.get("season")) != str(spec["year"])
            or params.get("pageType") != ("standings" if kind == "standings" else "team")
            or kind != "standings" and (str(params.get("id")) != spec["team_id"] or params.get("subPageType") != kind or page.get("subType") != kind)):
        _fail("的页面球队、联赛或赛季与请求不一致")
    content = _object(_object(page.get("content")).get(kind))
    normalizer = _roster if kind == "squad" else _standings if kind == "standings" else _schedule
    result = normalizer(content, spec)
    result["_touchline_source"] = {"method": "espn_public_page", "url": spec["url"], "fetched_at": now()}
    return result


async def fetch_public_payload(spec):
    # The URL comes only from fallback_spec, never from page content or a model.
    url = spec["url"]
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "www.espn.com" or parsed.username or parsed.password or parsed.port not in {None, 443}:
        _fail("地址不在允许范围")
    try:
        async with asyncio.timeout(15):
            async with httpx.AsyncClient(timeout=httpx.Timeout(13, connect=8), follow_redirects=False,
                    headers={"User-Agent": "Touchline/7.1 (personal football reader)", "Accept": "text/html"}) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        _fail(f"返回 HTTP {response.status_code}")
                    if "text/html" not in response.headers.get("content-type", ""):
                        _fail("没有返回 HTML 资料")
                    pieces, size = [], 0
                    async for piece in response.aiter_bytes():
                        size += len(piece)
                        if size > MAX_BODY:
                            _fail("超过读取上限")
                        pieces.append(piece)
        return parse_page(b"".join(pieces), spec)
    except IngestionError:
        raise
    except (httpx.HTTPError, TimeoutError):
        _fail("暂不可访问")
