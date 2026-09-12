"""Bounded, club-specific adapters for public first-team fixture sources.

No model or third-party football analysis service is involved. Arsenal's new
website exposes its own raw fixture records via public read-only GraphQL; the
other adapters parse data already delivered in the official HTML response.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from . import ingestion, research

CACHE_SECONDS = 30 * 60
MAX_FIXTURES = 160
ARSENAL_TEAM_ID = "4dsgumo7d4zupm2ugsvm4zm4d"  # Men's first team, from official contestant ID.
ARSENAL_GRAPH = "https://afc-prd.graph.arsenal.com/graphql"
SOURCES = {
    "arsenal": {"name": "Arsenal FC 官方赛程", "url": "https://www.arsenal.com/fixtures/men/fixtures"},
    "liverpool": {"name": "Liverpool FC 官方赛程", "url": "https://www.liverpoolfc.com/matches/mens-team/fixtures"},
    "barcelona": {"name": "FC Barcelona 官方赛程", "url": "https://www.fcbarcelona.com/en/football/first-team/schedule"},
    "inter": {"name": "Inter 官方赛程", "url": "https://www.inter.it/en/matches?category=upcoming&competition=all&teams=men"},
}
_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _walk(value, depth=0):
    if depth > 55:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child, depth + 1)
    elif isinstance(value, list):
        for child in value[:10000]:
            yield from _walk(child, depth + 1)


def _flight(soup):
    """Decode inert Next Flight JSON records, never evaluate JavaScript."""
    for script in soup.find_all("script"):
        text = script.get_text().strip()
        if not text.startswith("self.__next_f.push("):
            continue
        try:
            record = json.JSONDecoder().raw_decode(text[len("self.__next_f.push("):])[0]
            payload = record[1]
            if not isinstance(payload, str):
                continue
            for line in payload.splitlines():
                raw = line.partition(":")[2]
                if raw.startswith(("{", "[")):
                    try:
                        yield json.loads(raw)
                    except (ValueError, RecursionError):
                        continue
        except (ValueError, IndexError, TypeError, RecursionError):
            continue


def _score(value):
    if isinstance(value, bool) or value is None or not re.fullmatch(r"\d{1,2}", str(value)):
        return None
    return int(value)


def _time(value, *, assume_utc=False):
    if not isinstance(value, str) or "T" not in value:
        return ""
    if assume_utc and not re.search(r"(?:Z|[+-]\d\d:\d\d)$", value):
        value += "+00:00"
    return ingestion.published_time(value) or ""


def _status(raw):
    text = str(raw or "").lower().replace(" ", "")
    if any(x in text for x in ("postpon", "abandon", "suspend", "cancel")):
        return "cancelled" if "cancel" in text else "postponed"
    if text in {"played", "ended", "complete", "completed", "fulltime", "ft", "postmatch", "finished"}:
        return "finished"
    if text in {"playing", "live", "inprogress", "halftime"}:
        return "live"
    return "scheduled"


def _base(team_id, match_id, home, away, kickoff, raw_status, *, home_score=None, away_score=None,
          confirmed=True, date_only="", competition="", stadium="", source_url=None,
          source_updated_at=None, warnings=None, raw=None):
    known = {"arsenal": {"Arsenal"}, "liverpool": {"Liverpool", "Liverpool FC"},
             "barcelona": {"FC Barcelona", "Barcelona"}, "inter": {"Inter", "Internazionale"}}
    if not match_id or (home not in known[team_id] and away not in known[team_id]):
        return None
    # Entity scope cannot be inferred from a team-name substring.
    if re.search(r"\b(?:women|u\d\d|under[ -]?\d\d|youth|academy|femeni|femení)\b", f"{home} {away} {competition}", re.I):
        return None
    venue = "home" if home in known[team_id] else "away"
    state = _status(raw_status)
    warnings = list(warnings or [])
    if state in {"cancelled", "postponed"}:
        confirmed = False
        warnings.append("官方状态：比赛已取消" if state == "cancelled" else "官方状态：比赛延期或中止，等待重新确认")
    kickoff = _time(kickoff)
    confirmed = bool(confirmed and kickoff)
    if not confirmed:
        warnings.append("开球日期或时间尚未确认，请以官方后续公告为准")
    hs, aws = _score(home_score), _score(away_score)
    if state != "finished":
        hs = aws = None  # In-play snapshots are not final results.
    if state == "finished" and (hs is None or aws is None):
        warnings.append("官网已标记完赛，但没有可读取的完整比分")
    gf, ga = (hs, aws) if venue == "home" else (aws, hs)
    digest = hashlib.sha256(f"{team_id}:{match_id}".encode()).hexdigest()[:18]
    return {"id": "match_live_" + digest, "mode": "live", "team_id": team_id,
            "opponent": away if venue == "home" else home, "home_team": home, "away_team": away,
            "venue": venue, "stadium": stadium or "", "competition": competition or "赛事待确认",
            "kickoff": kickoff, "kickoff_date": date_only or kickoff[:10], "kickoff_confirmed": confirmed,
            "status": "finished" if state == "finished" else "scheduled", "fixture_status": state,
            "score": f"{gf}–{ga}" if gf is not None and ga is not None else None,
            "home_score": hs, "away_score": aws, "goals_for": gf, "goals_against": ga,
            "result": ("W" if gf > ga else "L" if gf < ga else "D") if gf is not None and ga is not None else None,
            "source_match_id": str(match_id), "source_url": source_url or SOURCES[team_id]["url"],
            "source_name": SOURCES[team_id]["name"], "source_updated_at": _time(source_updated_at),
            "data_warnings": list(dict.fromkeys(warnings)), "lineups": {"status": "unavailable"},
            "availability": [], "evidence_ids": [], "note": "", "source_raw": raw or {}}


def parse_arsenal(data):
    if not isinstance(data, dict) or data.get("errors"):
        raise ingestion.IngestionError("阿森纳官方数据查询未成功，未使用不完整响应")
    data = data.get("data") or {}
    fixtures = {n.get("matchInfo", {}).get("id"): n for n in (data.get("fixturesByIds") or {}).get("matches", [])}
    result = []
    for node in (data.get("getLiveReportsListByFilters") or {}).get("data", []):
        custom = node.get("customTypeData") or {}
        details = custom.get("fixtureDetails") or {}
        team = details.get("arsenalTeam") or {}
        if team.get("slug") != "men":
            continue
        opta_id = (custom.get("fixtureConfig") or {}).get("optaId")
        feed = fixtures.get(opta_id, {})
        info = feed.get("matchInfo") or {}
        contestants = info.get("contestant") or []
        if contestants and not any(t.get("id") == ARSENAL_TEAM_ID for t in contestants):
            continue
        # The official editorial fields omit a suffix; only trust them as UTC
        # when they agree with the same official match's explicitly-Z fields.
        feed_date, feed_time = info.get("date", ""), info.get("time", "")
        explicit_utc = _time(feed_date.rstrip("Z") + "T" + feed_time) if feed_date.endswith("Z") and feed_time.endswith("Z") else ""
        editorial = details.get("dateOverride") or details.get("fixtureDate") or ""
        editorial_utc = _time(editorial, assume_utc=True)
        verified_time = bool(explicit_utc and explicit_utc == editorial_utc)
        warnings = [] if verified_time else ["官网日期字段尚未完成 UTC 交叉核对"]
        label = (details.get("matchStatus") or {}).get("label") or node.get("matchState")
        confirmed = verified_time and not re.search(r"tbc|tba|provisional|unconfirmed", label or "", re.I)
        venue = details.get("venueType1")
        if venue not in {"home", "away", "neutral"}:
            continue
        opponent = (details.get("opponent") or {}).get("clubName") or (details.get("opponent") or {}).get("name")
        home, away = (opponent, "Arsenal") if venue == "away" else ("Arsenal", opponent)
        hs, aws = (details.get("scoreOpponent"), details.get("scoreArsenal")) if venue == "away" else (details.get("scoreArsenal"), details.get("scoreOpponent"))
        feed_scores = ((feed.get("liveData") or {}).get("matchDetails") or {}).get("scores") or {}
        feed_scores = feed_scores.get("total") or {}
        if _status(node.get("matchState")) == "finished" and feed_scores and (
                _score(hs) != _score(feed_scores.get("home")) or _score(aws) != _score(feed_scores.get("away"))):
            warnings.append("官网编辑资料与赛事字段比分冲突，暂不展示赛果或计入图表")
            hs = aws = None
        match_slug = (node.get("seo") or {}).get("slug")
        match_url = SOURCES["arsenal"]["url"]
        if match_slug and re.fullmatch(r"[a-zA-Z0-9-]+", match_slug):
            match_url += "/match/" + match_slug + "-" + str(node["id"])
        state = label if _status(label) in {"cancelled", "postponed"} else node.get("matchState")
        row = _base("arsenal", opta_id or node.get("id"), home, away, explicit_utc if verified_time else "", state,
                    home_score=hs, away_score=aws, confirmed=confirmed, date_only=editorial[:10],
                    competition=(info.get("competition") or {}).get("name", ""), stadium=details.get("venue"),
                    source_url=match_url, source_updated_at=info.get("lastUpdated"), warnings=warnings,
                    raw={"editorial": node, "fixture": feed})
        if row:
            row["data_url"] = ARSENAL_GRAPH
            if venue == "neutral":
                row["venue"] = "neutral"
            result.append(row)
    return result


def parse_inter(body, url):
    soup = BeautifulSoup(body, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        raise ingestion.IngestionError("国米官网未包含赛程结构化数据")
    data = json.loads(tag.get_text())
    if data.get("query", {}).get("teams", "men") != "men":
        raise ingestion.IngestionError("国米来源不是男足一线队")
    page = data.get("props", {}).get("pageProps", {}).get("matchesPageData", {})
    archive = re.search(r"/matches/(\d{4})-(\d{4})", urlsplit(url).path)
    if archive and page.get("activeSeasonName") != archive[1] + "/" + archive[2][-2:]:
        raise ingestion.IngestionError("国米官网未返回请求的历史赛季，保留已有赛果")
    result = []
    for node in _walk(page.get("allMatchesSeasonData", [])):
        if not {"id", "teams", "status", "date"} <= node.keys():
            continue
        teams, scores = node["teams"], node.get("score") or {}
        home, away = teams.get("homeTeam", {}), teams.get("awayTeam", {})
        # This ID is the men's team declared by Inter's own Men selector.
        if not any(t.get("id") == "3vo5mpj7catp66nrwwqiuhuup" for t in (home, away)):
            continue
        row = _base("inter", node["id"], home.get("name"), away.get("name"), node["date"], node["status"],
                    home_score=scores.get("homeTeam"), away_score=scores.get("awayTeam"),
                    confirmed=node.get("isDateConfirmed") is True, date_only=node["date"][:10],
                    competition=(node.get("competition") or {}).get("name"), stadium=node.get("location"),
                    source_url=url, raw={k: v for k, v in node.items() if k != "playerEvents"})
        if row:
            result.append(row)
    return result


def parse_barcelona(body, url):
    if "/football/first-team/" not in url:
        raise ingestion.IngestionError("巴萨来源不是足球一线队赛程页")
    soup = BeautifulSoup(body, "html.parser")
    states = {n.get("url"): n for data in ingestion.embedded_json(soup) for n in _walk(data) if n.get("@type") == "SportsEvent"}
    result, seen = [], set()
    for card in soup.select(".fixture-result-list__fixture"):
        link = card.select_one("a.fixture-result-list__link")
        if not link or not re.match(r"/en/matches/\d+/", link.get("href", "")):
            continue
        target = urljoin(url, link["href"])
        if target in seen:
            continue
        seen.add(target)
        match_id = re.search(r"/matches/(\d+)/", target).group(1)
        text = lambda selector: (tag.get_text(" ", strip=True) if (tag := card.select_one(selector)) else "")
        home, away = text(".fixture-info__name--home"), text(".fixture-info__name--away")
        state = states.get(target, {}).get("eventStatus") or ("Complete" if "/results" in url else "Upcoming")
        stamp = card.select_one("[data-fixture-date]")
        kickoff = ""
        if stamp and re.fullmatch(r"\d{13}", stamp.get("data-fixture-date", "")):
            kickoff = datetime.fromtimestamp(int(stamp["data-fixture-date"]) / 1000, timezone.utc).isoformat()
        provisional = bool(card.select_one(".fixture-info__time--provisional, .fixture-result-list__tbc-explainer"))
        date_only = states.get(target, {}).get("startDate", "")[:10]
        goals = re.fullmatch(r"\s*(\d+)\s*[-–:]\s*(\d+)\s*", text(".fixture-info__score"))
        row = _base("barcelona", match_id, home, away, "" if provisional else kickoff, state,
                    confirmed=not provisional, date_only=date_only,
                    home_score=goals.group(1) if goals else None, away_score=goals.group(2) if goals else None,
                    competition=text(".fixture-result-list__name"), stadium=text(".fixture-result-list__stage-location"),
                    source_url=target, raw={"schema": states.get(target, {}), "displayed_card": card.get_text(" ", strip=True),
                                            "date_milliseconds": stamp.get("data-fixture-date") if stamp else None})
        if row:
            result.append(row)
    return result


def parse_liverpool(body, url):
    if "/matches/mens-team/" not in url:
        raise ingestion.IngestionError("利物浦来源不是男足一线队")
    soup = BeautifulSoup(body, "html.parser")
    visible = soup.select('article[data-testid="fixture-card-slotted"]')
    cards, seen = [], set()
    for data in _flight(soup):
        for node in _walk(data):
            if node.get("data-testid") != "fixture-card-slotted":
                continue
            descendants = list(_walk(node))
            date = next((n for n in descendants if "kickOffUtc" in n), {})
            teams = [n["children"] for n in descendants if "__teamName__" in n.get("className", "") and isinstance(n.get("children"), str)]
            if len(teams) != 2:
                continue
            key = (date.get("kickOffUtc"), *teams)
            if key in seen:
                continue
            seen.add(key)
            cards.append((node, date, teams, descendants))
    result = []
    for index, (_, date, teams, nodes) in enumerate(cards):
        if index >= len(visible):
            continue
        card = visible[index]
        # Match Flight records with their server-rendered team names before
        # borrowing the corresponding original match URL or competition.
        dom_teams = [n.get_text(" ", strip=True) for n in card.select('[class*="__teamName__"]')]
        if teams != dom_teams:
            continue
        links = [a["href"] for a in card.select("a[href]") if "/matches/mens-team/" in a["href"]]
        if not links:
            continue
        target = urljoin(url, links[0])
        match_id = urlsplit(target).path.rstrip("/").rsplit("/", 1)[-1]
        scores = [n.get("children") for n in nodes if "__score__" in n.get("className", "")]
        competition = card.select_one('[class*="__competitionInline__"]')
        stadium = card.select_one('[class*="__stadium__"]')
        confirmed = not date.get("dateTbc", True) and not date.get("timeTbc", True)
        display = card.get_text(" ", strip=True)
        state = "FullTime" if "/results/" in url and len(scores) == 2 else "scheduled"
        if re.search(r"\b(postponed|cancelled|abandoned)\b", display, re.I):
            state = re.search(r"\b(postponed|cancelled|abandoned)\b", display, re.I).group(1)
        row = _base("liverpool", match_id, teams[0], teams[1], date.get("kickOffUtc", "") if confirmed else "", state,
                    confirmed=confirmed, date_only=date.get("kickOffUtc", "")[:10],
                    home_score=scores[0] if len(scores) == 2 else None, away_score=scores[1] if len(scores) == 2 else None,
                    competition=competition.get_text(" ", strip=True) if competition else "",
                    stadium=stadium.get_text(" ", strip=True) if stadium else "", source_url=target,
                    raw={"date": date, "teams": teams, "scores": scores, "displayed_card": display})
        if row:
            result.append(row)
    return result


def parse_liverpool_lineups(body, match, url):
    soup = BeautifulSoup(body, "html.parser")
    for data in _flight(soup):
        for node in _walk(data):
            if not all(k in node for k in ("homeLineUpLiveProps", "awayLineUpLiveProps")):
                continue
            sides = [node["homeLineUpLiveProps"], node["awayLineUpLiveProps"]]
            if not all(isinstance(side, dict) and len(side.get("starting", [])) == 11 for side in sides):
                continue
            # Team shortName handles Ipswich / Ipswich Town in the same source.
            if any(match[key] not in {side.get("name"), side.get("shortName")} for key, side in zip(("home_team", "away_team"), sides)):
                continue
            values = []
            for side in sides:
                names = [p.get("knownAs") or " ".join(filter(None, (p.get("firstName"), p.get("lastName")))) for p in side["starting"]]
                if any(not name for name in names) or len(set(names)) != 11:
                    break
                values.append(names)
            if len(values) == 2:
                return {"status": "confirmed", "home": values[0], "away": values[1], "source_url": url,
                        "updated_at": ingestion.now()}
    return {"status": "unavailable"}


def parse_arsenal_lineups(article, match):
    """Read explicitly labelled XI lists from a same-match official bulletin."""
    published, kickoff = _time(article.get("published_at")), _time(match.get("kickoff"))
    if not published or not kickoff or not -4 * 3600 <= (datetime.fromisoformat(published) - datetime.fromisoformat(kickoff)).total_seconds() <= 3600:
        return {"status": "unavailable"}
    text = article.get("text", "")
    heading = re.search(r"(?im)^LINE[ -]?UPS\s*$", text)
    if not heading:
        return {"status": "unavailable"}
    text = text[heading.end():]
    values = []
    for side in (match["home_team"], match["away_team"]):
        names = re.search(r"(?im)^" + re.escape(side) + r":\s*([^\n]+)", text)
        if not names:
            return {"status": "unavailable"}
        squad = [name.strip().rstrip(".") for name in names[1].split(",")]
        if len(squad) != 11 or len(set(squad)) != 11 or any(not name or len(name) > 70 for name in squad):
            return {"status": "unavailable"}
        values.append(squad)
    return {"status": "confirmed", "home": values[0], "away": values[1],
            "source_url": article["url"], "updated_at": published}


async def _enrich_arsenal_lineups(rows, current):
    past = [row for row in rows if row["status"] == "finished"][:1]
    upcoming = [row for row in rows if row["status"] != "finished" and row["kickoff"]
                and abs((datetime.fromisoformat(row["kickoff"]) - current).total_seconds()) < 48 * 3600][:1]
    targets = past + upcoming
    if not targets:
        return
    source = {"id": "official_arsenal", "url": SOURCES["arsenal"]["url"], "builtin": True}
    try:
        body, _, _ = await research._fetch("https://www.arsenal.com/sitemaps/articles/1/sitemap.xml", source)
        candidates = research._sitemap_candidates(body)
    except (ingestion.IngestionError, asyncio.TimeoutError):
        return
    for row in targets:
        # Names come from the already scoped official fixture; no fuzzy player
        # matching or article URL date guessing is used for same-match linkage.
        tokens = [part.lower() for part in re.findall(r"[A-Za-z]{4,}", row["opponent"])]
        links = [c["url"] for c in candidates if "/news/team-news-" in c["url"]
                 and any(token in c["url"].lower() for token in tokens)
                 and not re.search(r"women|u18|u19|u21|academy", c["url"], re.I)][:2]
        for url in links:
            try:
                body, final, _ = await research._fetch(url, source)
                article = ingestion.extract_html(body, final, "Arsenal FC 官方")[0]
                lineup = parse_arsenal_lineups(article, row)
                if lineup["status"] == "confirmed":
                    row["lineups"] = lineup
                    row["source_raw"]["lineup_article"] = article
                    break
            except (ingestion.IngestionError, asyncio.TimeoutError):
                row["data_warnings"].append("官方首发公告暂时无法读取")


async def _fetch_page(team_id, url):
    source = {**SOURCES[team_id], "id": "official_" + team_id, "builtin": True}
    research._checked_url(url, research._source_hosts(source))
    # Liverpool's archived complete-season HTML can exceed the normal article
    # limit. Only this fixed official adapter may use the bounded 8 MiB limit.
    if team_id == "liverpool":
        return await asyncio.wait_for(asyncio.to_thread(ingestion.fetch_document, url,
                                      allowed_hosts=research._source_hosts(source), max_body=8 * 1024 * 1024), 25)
    return await research._fetch(url, source)


def _arsenal_query_sync(month):
    if not re.fullmatch(r"\d{4}-\d{2}-01", month):
        raise ingestion.IngestionError("赛程月份格式错误")
    # Fixed public SELECT-equivalent operation only. No introspection,
    # credentials, mutations, arbitrary endpoints, or redirects are accepted.
    query = '''query($date:String!,$range:MonthDay!) {
      getLiveReportsListByFilters(date:$date,competitions:"all",rangeType:$range,teamIds:"TEAM",timeOffset:0) {
        data { id matchState customTypeData { fixtureConfig { optaId }
          fixtureDetails { scoreArsenal scoreOpponent venueType1 venue fixtureDate dateOverride
            matchStatus { label } arsenalTeam { name clubName slug } opponent { name clubName slug } } }
          seo { slug } } }
      fixturesByIds(date:$date,competitions:"all",rangeType:"month",teamIds:"TEAM",timeOffset:0) {
        matches { matchInfo { id date time lastUpdated competition { name }
          contestant { id name position } venue { longName } }
          liveData { matchDetails { matchStatus scores { total { home away } } } } } }
    }'''.replace("TEAM", ARSENAL_TEAM_ID)
    host = urlsplit(ARSENAL_GRAPH).hostname
    connection = ingestion._PinnedHTTP(host, 443, ingestion.public_addresses(host, 443)[0], True, 10)
    started = time.monotonic()
    try:
        body = json.dumps({"query": query, "variables": {"date": month, "range": "month"}}).encode()
        connection.request("POST", "/graphql", body=body,
                           headers={"Host": host, "Content-Type": "application/json", "Accept": "application/json",
                                    "Accept-Encoding": "identity", "User-Agent": "Touchline/3.0 personal fixture reader"})
        response = connection.getresponse()
        if response.status != 200:
            raise ingestion.IngestionError(f"阿森纳官方赛程返回 HTTP {response.status}")
        if "application/json" not in response.getheader("Content-Type", ""):
            raise ingestion.IngestionError("阿森纳官方赛程没有返回 JSON")
        if response.getheader("Content-Encoding", "identity") not in {"", "identity"}:
            raise ingestion.IngestionError("阿森纳官方赛程返回不支持的压缩格式")
        chunks, size = [], 0
        while True:
            if time.monotonic() - started > 20:
                raise ingestion.IngestionError("阿森纳官方赛程请求超时")
            chunk = response.read(min(65536, ingestion.MAX_BODY + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > ingestion.MAX_BODY:
                raise ingestion.IngestionError("阿森纳官方赛程超过 2 MB 上限")
            chunks.append(chunk)
        return json.loads(b"".join(chunks))
    except ingestion.IngestionError:
        raise
    except Exception:
        raise ingestion.IngestionError("无法安全读取阿森纳官网公开赛程") from None
    finally:
        connection.close()


def _month_shift(current, delta):
    index = current.year * 12 + current.month - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}-01"


async def _collect(team_id):
    current = datetime.now(timezone.utc)
    rows, errors, source_records = [], [], []
    semaphore = asyncio.Semaphore(3)
    async def one(url, parser, month=None):
        async with semaphore:
            try:
                if month:
                    data = await asyncio.wait_for(asyncio.to_thread(_arsenal_query_sync, month), 25)
                    parsed, final = parse_arsenal(data), url
                else:
                    body, final, _ = await _fetch_page(team_id, url)
                    parsed = parser(body, final)
                if not parsed and not month:
                    raise ingestion.IngestionError("官方页面没有可识别的一线队比赛，未替换已有数据")
                rows.extend(parsed)
                source_records.append({"url": final, "month": month, "matches": len(parsed), "status": "ok"})
            except (ingestion.IngestionError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as exc:
                error = str(exc) if isinstance(exc, ingestion.IngestionError) else "来源读取超时或页面结构已变化"
                errors.append(f"{SOURCES[team_id]['name']}：{error}")
                source_records.append({"url": url, "month": month, "matches": 0, "status": "error", "error": error})
    if team_id == "arsenal":
        await asyncio.gather(*(one(ARSENAL_GRAPH, None, _month_shift(current, delta)) for delta in (-3, -2, -1, 0, 1)))
    elif team_id == "liverpool":
        year = current.year if current.month >= 7 else current.year - 1
        await asyncio.gather(one(SOURCES[team_id]["url"], parse_liverpool),
                             one(f"https://www.liverpoolfc.com/matches/mens-team/results/{year}", parse_liverpool))
        if sum(r["status"] == "finished" for r in rows) < 10:
            await one(f"https://www.liverpoolfc.com/matches/mens-team/results/{year - 1}", parse_liverpool)
    elif team_id == "barcelona":
        await asyncio.gather(one(SOURCES[team_id]["url"], parse_barcelona),
                             one("https://www.fcbarcelona.com/en/football/first-team/results", parse_barcelona))
    elif team_id == "inter":
        await asyncio.gather(one(SOURCES[team_id]["url"], parse_inter),
                             one("https://www.inter.it/en/matches?category=results&competition=all&teams=men", parse_inter))
        if sum(r["status"] == "finished" for r in rows) < 10:
            year = current.year if current.month >= 7 else current.year - 1
            await one(f"https://www.inter.it/en/matches/{year - 1}-{year}?category=results&competition=all&teams=men", parse_inter)
    unique = {}
    for row in rows:
        key = row["source_match_id"]
        if key not in unique or row["status"] == "finished":
            unique[key] = row
    rows = list(unique.values())
    past = sorted([r for r in rows if r["status"] == "finished"], key=lambda r: r["kickoff_date"], reverse=True)[:30]
    upcoming = sorted([r for r in rows if r["status"] != "finished"], key=lambda r: r["kickoff_date"])[:100]
    rows = past + upcoming
    if team_id == "arsenal":
        await _enrich_arsenal_lineups(rows, current)
    if team_id == "liverpool":
        targets = past[:1] + [r for r in upcoming[:1] if r["kickoff"] and abs((datetime.fromisoformat(r["kickoff"]) - current).total_seconds()) < 48 * 3600]
        for row in targets:
            try:
                body, final, _ = await _fetch_page(team_id, row["source_url"])
                row["lineups"] = parse_liverpool_lineups(body, row, final)
            except (ingestion.IngestionError, asyncio.TimeoutError):
                row["data_warnings"].append("官方比赛页暂时无法读取首发名单")
    return rows[:MAX_FIXTURES], errors, source_records


_SEMANTIC = ("opponent", "home_team", "away_team", "venue", "stadium", "competition", "kickoff", "kickoff_date",
             "kickoff_confirmed", "status", "fixture_status", "score", "home_score", "away_score", "goals_for", "goals_against",
             "result", "data_warnings")


def _non_result_fields(value):
    if isinstance(value, dict):
        return {key: _non_result_fields(child) for key, child in value.items()
                if not re.search(r"score|goals|result", key, re.I) and key not in {"displayed_card", "lineup_article"}}
    if isinstance(value, list):
        return [_non_result_fields(child) for child in value]
    return value


def _evidence(store, row, at):
    # A deterministic extraction record; no generated narrative or guessed
    # squad. Preserve original fields so each displayed value is reproducible.
    exported = {key: row.get(key) for key in _SEMANTIC}
    exported["source_match_id"] = row["source_match_id"]
    if row["lineups"].get("status") == "confirmed":
        exported["lineups"] = {key: row["lineups"].get(key) for key in ("status", "home", "away", "source_url")}
    text = "官方男足一线队比赛记录（由公开原始字段提取）\n" + json.dumps(exported, ensure_ascii=False, indent=2)
    raw = row.get("source_raw", {})
    if row["fixture_status"] != "finished":
        # Public feeds sometimes carry 0–0 placeholders before a game. The full
        # forensic snapshot stays on the match, outside the model-facing view.
        raw = _non_result_fields(raw)
        text += "\n比赛尚未完场。以下原始来源字段已排除比分占位和直播数字，不能据此推断赛果。"
    text += "\n原始来源字段：\n" + json.dumps(raw, ensure_ascii=False, indent=2)
    fingerprint = ingestion.fingerprint(text)
    identifier = "ev_fixture_" + hashlib.sha256((row["team_id"] + fingerprint).encode()).hexdigest()[:18]
    if store.get("evidence", identifier):
        return identifier, 0
    evidence = {"id": identifier, "mode": "live", "team_id": row["team_id"],
                "title": f"{row['home_team']} vs {row['away_team']} · 官方比赛资料",
                "text": text[:30000], "url": row["source_url"], "source_name": row["source_name"],
                "published_at": None, "source_updated_at": row.get("source_updated_at") or None, "fetched_at": at,
                "origin": "web", "spoiler": row["status"] == "finished" or row["fixture_status"] == "live",
                "fingerprint": fingerprint, "fixture_record": True, "match_id": row["id"]}
    store.put("evidence", evidence)
    return identifier, 1


def save_matches(store, team_id, rows, at):
    updated = added = lineups_updated = 0
    with store.atomic():
        existing = store.list("matches", "live", team_id)
        by_source = {r.get("source_match_id"): r for r in existing if r.get("source_match_id")}
        for row in rows:
            old = by_source.get(row["source_match_id"]) or store.get("matches", row["id"])
            if not old:
                # Adopt a manually-created instance only when teams, venue and
                # exact kickoff agree, retaining its ID, notes and associations.
                old = next((r for r in existing if not r.get("source_match_id") and r.get("opponent") == row["opponent"]
                            and row["kickoff"] and r.get("kickoff") == row["kickoff"] and r.get("venue") == row["venue"]), None)
            if old:
                source_old, source_new = old.get("source_updated_at"), row.get("source_updated_at")
                if source_old and source_new and source_new < source_old:
                    continue
                if old.get("status") == "finished" and row["status"] != "finished":
                    continue  # Stale fixture listing must not undo a final result.
                row["id"], row["note"] = old["id"], old.get("note", "")
                row["evidence_ids"] = list(old.get("evidence_ids", []))
                row["availability"] = old.get("availability", [])
                if row["lineups"].get("status") != "confirmed" and (old.get("lineups") or {}).get("status") == "confirmed":
                    row["lineups"] = old["lineups"]
            changed = {key: {"before": old.get(key) if old else None, "after": row.get(key)} for key in _SEMANTIC if not old or old.get(key) != row.get(key)}
            lineup_new = {k: row["lineups"].get(k) for k in ("status", "home", "away")}
            lineup_old = {k: (old.get("lineups") or {}).get(k) for k in ("status", "home", "away")} if old else {}
            if lineup_new != lineup_old and row["lineups"]["status"] == "confirmed":
                lineups_updated += 1
                changed["lineups"] = {"before": lineup_old, "after": lineup_new}
            if changed:
                updated += 1
                ev_id, count = _evidence(store, row, at)
                added += count
                if ev_id not in row["evidence_ids"]:
                    row["evidence_ids"].append(ev_id)
                versions = list(old.get("source_versions", [])) if old else []
                versions.append({"at": at, "source_url": row["source_url"], "source_updated_at": row.get("source_updated_at"),
                                 "changes": changed, "evidence_id": ev_id})
                row["source_versions"] = versions[-20:]
                row["updated_at"] = at
            elif old:
                row["source_versions"] = old.get("source_versions", [])
                row["updated_at"] = old.get("updated_at", at)
            row["synced_at"] = at
            # Preserve user-added fields not owned by the adapter.
            row = {**(old or {}), **row}
            store.put("matches", row)
            by_source[row["source_match_id"]] = row
    return updated, added, lineups_updated


def get_team_status(store, team_id):
    cached = store.get_meta("fixtures_sync_" + team_id, {})
    at = _time(cached.get("synced_at"))
    stale = not at or (datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds() >= CACHE_SECONDS
    return {"supported": team_id in SOURCES, "source": SOURCES.get(team_id), **cached,
            "stale": stale, "cache_seconds": CACHE_SECONDS}


async def sync_team(store, team_id, force=False):
    if not store.get("teams", team_id):
        raise ingestion.IngestionError("球队不存在")
    lock = _locks.setdefault((id(store), team_id), asyncio.Lock())
    async with lock:
        cached = get_team_status(store, team_id)
        if not force and not cached["stale"] and not cached.get("errors"):
            return {**{k: cached.get(k) for k in ("errors", "synced_at", "sources")}, "matches_updated": 0,
                    "evidence_added": 0, "lineups_updated": 0, "cached": True}
        at = ingestion.now()
        if team_id not in SOURCES:
            result = {"matches_updated": 0, "evidence_added": 0, "lineups_updated": 0,
                      "errors": ["该球队尚未有经过核对的官方赛程适配器"], "synced_at": at, "sources": []}
        else:
            rows, errors, sources = await _collect(team_id)
            if not rows and not errors:
                errors.append("官方来源未返回比赛；已有数据已保留")
            updated, added, lineups_updated = save_matches(store, team_id, rows, at)
            result = {"matches_updated": updated, "evidence_added": added, "lineups_updated": lineups_updated,
                      "errors": errors, "synced_at": at, "sources": sources, "matches_available": len(rows)}
        store.set_meta("fixtures_sync_" + team_id, result)
        return result
