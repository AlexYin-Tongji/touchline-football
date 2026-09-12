"""Season-scoped Big Five data, sourced from ESPN's public website feeds.

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

import httpx

from .ingestion import IngestionError, now, published_time

API = "https://site.api.espn.com"
MAX_BODY = 16 * 1024 * 1024
CACHE_SECONDS = 1800


async def fetch_json(url):
    if urlsplit(url).hostname != "site.api.espn.com" or not url.startswith(API + "/apis/"):
        raise IngestionError("比赛数据仅可访问固定的公开数据来源。")
    try:
        async with asyncio.timeout(45):
            async with httpx.AsyncClient(timeout=httpx.Timeout(35, connect=12), follow_redirects=False,
                                         headers={"User-Agent": "Touchline/6.0 (personal football reader)", "Accept": "application/json"}) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
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
    except (httpx.TimeoutException, TimeoutError):
        raise IngestionError("比赛来源响应超时，已保留上次数据。") from None
    except (httpx.HTTPError, ValueError):
        raise IngestionError("比赛数据暂不可读，请稍后重试。") from None


def season_context(store, league_id, year=None):
    league = store.get("leagues", league_id)
    if not league:
        raise IngestionError("联赛不存在。")
    seasons = [item for item in store.list("seasons") if item["league_id"] == league_id]
    season = next((s for s in seasons if s["season_start_year"] == year), None) if year is not None else max(seasons, key=lambda s: s["season_start_year"], default=None)
    if not season:
        raise IngestionError("这个赛季尚未建立可核对的参赛球队目录。")
    members = [m for m in store.list("league_memberships") if m["season_id"] == season["id"]]
    mapping = {str(m["provider_team_id"]): m["team_id"] for m in members}
    if len(mapping) != league["expected_team_count"]:
        raise IngestionError("该联赛的赛季球队目录不完整，暂不导入比赛。")
    return league, season, mapping


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
    errors, updated = [], 0
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
        except asyncio.CancelledError:
            raise
        except (IngestionError, ValueError, TypeError, KeyError) as exc:
            errors.append(("赛程：" if kind == "fixtures" else "积分：") + str(exc))
        except Exception:
            errors.append(("赛程" if kind == "fixtures" else "积分") + "来源暂不可用，已保留上次数据。")
    status = {**prior, "sync_status": "completed" if not errors else "partial" if updated else "failed", "errors": errors, "last_attempt": now()}
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
        evidence_text = "ESPN 男足联赛比赛资料（公开数据字段，非模型生成）\n" + json.dumps({k: v for k, v in source.items() if k != "fetched_at"}, ensure_ascii=False, indent=2)
        evidence_id = "ev_league_" + hashlib.sha256((team_id + evidence_text).encode()).hexdigest()[:22]
        if not store.get("evidence", evidence_id):
            store.put("evidence", {"id": evidence_id, "mode": "live", "team_id": team_id, "text": evidence_text,
                                   "title": f"{source['home_team']} vs {source['away_team']} · ESPN比赛资料", "fixture_record": True,
                                   "match_id": identity, "url": source["source_url"], "source_name": "ESPN 赛事数据", "origin": "web",
                                   "published_at": None, "fetched_at": source["fetched_at"], "spoiler": source["status"] in {"finished", "live"},
                                   "content_status": "full_text", "content_method": "structured_fixture", "fingerprint": hashlib.sha256(evidence_text.encode()).hexdigest()})
        updated = {**old, "id": identity, "mode": "live", "team_id": team_id, "opponent": opponent_name, "opponent_id": opponent_id,
                       "competition": league.get("name", source["league_id"]), "league_id": source["league_id"], "season_id": source["season_id"],
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
    result = await sync_league(store, member["league_id"], member["season_start_year"], force=force)
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
