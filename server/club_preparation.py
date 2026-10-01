"""Bounded, data-only preparation for any visited club, independent of following.

Viewing is an explicit POST from the UI. GET endpoints stay read-only; neither
preparation nor subsequent resident upkeep authorizes a paid model request.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .ingestion import now

RECENT_TEAMS_KEY = "recent_club_visits_v1"
RECENT_LIMIT = 16
RECENT_SECONDS = 7 * 86400


def _age(value, moment):
    try:
        stamp = datetime.fromisoformat(value)
        return (moment - stamp).total_seconds() if stamp.tzinfo else float("inf")
    except (ValueError, TypeError):
        return float("inf")


def recent_teams(store, *, moment=None):
    moment = moment or datetime.now(timezone.utc)
    records = store.get_meta(RECENT_TEAMS_KEY, {})
    if not isinstance(records, dict):
        return []
    return [team for team, stamp in sorted(records.items(), key=lambda item: str(item[1]), reverse=True)
            if store.get("teams", team) and 0 <= _age(stamp, moment) < RECENT_SECONDS][:RECENT_LIMIT]


class ClubPreparationManager:
    MAX_PENDING = 12
    FRESH_SECONDS = 1800
    RETRY_SECONDS = 300
    TIMEOUT_SECONDS = 180

    def __init__(self, store, team_maintenance, *, news_collector=None):
        self.store, self.team_maintenance = store, team_maintenance
        self.news_collector = news_collector
        self.tasks = {}
        self.gate = asyncio.Semaphore(2)

    def _key(self, kind, team_id):
        if kind not in {"team", "news"} or not self.store.get("teams", team_id):
            raise ValueError("功能或球队不存在。")
        return f"club_preparation_v1:{kind}:{team_id}"

    def status(self, kind, team_id):
        key = self._key(kind, team_id)
        row = self.store.get_meta(key, {})
        active = self.tasks.get((kind, team_id))
        status = row.get("status", "idle")
        # Restarted jobs are retryable; no stale marker can strand a cold club.
        if status == "running" and not (active and not active.done()):
            status = "interrupted"
        counts = dict(row.get("counts", {}))
        message = row.get("message", "打开球队后会准备来源资料。")
        sources, errors = [], []
        if kind == "news":
            from . import ingestion, research
            eligible = {item["id"]: item for item in research.source_catalog(self.store, team_id)}
            channel_ids = {"bing_news", "espn_news"}
            source_fields = ("id", "name", "state", "checked_at", "indexed_count", "relevant_count",
                             "dated_count", "window_count", "date_pending_count", "date_unknown_count")
            for source in row.get("sources", []):
                if source.get("id") in eligible or source.get("id") in channel_ids:
                    sources.append({key: source[key] for key in source_fields if key in source})
            for error in row.get("source_errors", []):
                publisher = research._publisher_source("https://" + str(error.get("host", "")))
                reviewed_search = bool(publisher and publisher["id"] == error.get("source_id"))
                if error.get("source_id") in eligible or error.get("source_id") in channel_ids or reviewed_search:
                    errors.append({key: error[key] for key in ("source_id", "source_name", "code", "stage", "message", "http_status") if key in error})
            if "evidence_ids" in row:
                visible = 0
                for identity in row["evidence_ids"]:
                    evidence = self.store.get("evidence", identity)
                    if not (research._readable_record(evidence) and evidence.get("team_id") == team_id and evidence.get("mode") == "live"):
                        continue
                    if evidence.get("origin") in {"web", "rss"}:
                        try:
                            research._read_source(self.store, evidence)
                        except ingestion.IngestionError:
                            continue
                    if self.store.settings().get("spoiler_free", True) and ingestion.safe_evidence_view(evidence) is None:
                        continue
                    if evidence.get("text"):
                        visible += 1
                counts["readable"] = visible
                counts["readable_count"] = visible
                if counts.get("readable") != row.get("counts", {}).get("readable") and status in {"ready", "partial"}:
                    message = (f"当前有 {visible} 份可读来源，可启动整理中文动态。" if visible else
                               "来源资料需要重新读取或已不可见，启动后会重新检查。")
                    status = "partial"
        return {"kind": kind, "team_id": team_id, "status": status, "message": message,
                "updated_at": row.get("updated_at"), "counts": counts, "sources": sources, "source_errors": errors}

    def start(self, kind, team_id, *, remember=True):
        key = self._key(kind, team_id)
        moment = datetime.now(timezone.utc)
        if remember:
            visits = self.store.get_meta(RECENT_TEAMS_KEY, {})
            visits = visits if isinstance(visits, dict) else {}
            visits[team_id] = moment.isoformat()
            self.store.set_meta(RECENT_TEAMS_KEY, dict(sorted(visits.items(), key=lambda item: str(item[1]), reverse=True)[:RECENT_LIMIT]))
        prior = self.status(kind, team_id)
        if self.store.get_meta("workflow:" + kind + ":" + team_id, {}).get("status") in {"running", "waiting", "stopping"}:
            return {**prior, "started": False, "message": "球队更新正在进行，完成后会显示。"}
        active = self.tasks.get((kind, team_id))
        if active and not active.done():
            return {**prior, "started": False}
        age = _age(prior.get("updated_at"), moment)
        cooldown = self.RETRY_SECONDS if prior["status"] in {"failed", "partial"} else self.FRESH_SECONDS
        if prior["status"] not in {"idle", "interrupted"} and 0 <= age < cooldown:
            return {**prior, "started": False}
        if sum(not task.done() for task in self.tasks.values()) >= self.MAX_PENDING:
            return {**prior, "started": False, "message": "正在准备其他球队资料，请稍后重试。"}
        row = {**prior, "status": "running", "updated_at": now(), "counts": {}, "sources": [], "source_errors": [],
               "message": "正在准备球队资料。" if kind == "team" else "正在查找消息并读取公开来源。"}
        self.store.set_meta(key, row)
        self.tasks[(kind, team_id)] = asyncio.create_task(self._run(kind, team_id))
        return {**row, "started": True}

    async def _run(self, kind, team_id):
        key = self._key(kind, team_id)
        try:
            async with self.gate:
                # Budget starts when a slot is acquired, not while waiting.
                async with asyncio.timeout(self.TIMEOUT_SECONDS):
                    if kind == "news":
                        from . import research
                        from .workflows import collection_summary
                        if self.store.get_meta("workflow:news:" + team_id, {}).get("status") in {"running", "waiting", "stopping"}:
                            self.store.set_meta(key, {"status": "partial", "message": "中文整理正在进行，完成后会显示。", "updated_at": now()})
                            return
                        collector = self.news_collector or research.collect_team
                        result = await collector(self.store, team_id, limit=4, max_attempts=6, max_age_days=7)
                        readable = len(result.get("evidence_ids", []))
                        summary = collection_summary(result)
                        counts = {**summary, "candidates": len(result.get("candidates", [])),
                                  "attempted_reads": result.get("attempted_reads", 0), "readable": readable,
                                  "added": result.get("added", 0)}
                        errors = result.get("source_errors", [])
                        status = summary["status"]
                        message = (f"已读到 {readable} 份消息来源，可启动整理中文动态。" if readable else
                                   "本轮来源未能读取，暂不能确认是否有新消息。" if status == "failed" else
                                   "找到的文章属于女足、青年队或其他范围，本轮未纳入男足一线队动态。" if summary["reason"] == "out_of_scope" else
                                   "已找到消息，但来源日期尚待确认。" if summary["reason"] in {"date_pending", "date_unknown"} else
                                   "已检查来源，本轮没有符合近期范围的消息。")
                        row = {"status": status, "message": message, "counts": counts,
                               "sources": result.get("sources", []), "source_errors": errors,
                               "evidence_ids": result.get("evidence_ids", [])}
                    else:
                        from .team_center import center_view
                        manual = self.store.get_meta("workflow:team:" + team_id, {})
                        if manual.get("status") in {"running", "waiting", "stopping"}:
                            row = {"status": "partial", "message": "球队资料更新正在进行，完成后会显示。", "counts": {}}
                        else:
                            started = self.team_maintenance.start(team_id)
                            if started.get("started") or started.get("status") == "running":
                                await self.team_maintenance.wait_for_team(team_id)
                            view = center_view(self.store, team_id)
                            players = view.get("roster", [])
                            counts = {"players": len(players), "photos": sum(bool(p.get("photo_url")) for p in players),
                                      "matches": len(view.get("matches", []))}
                            components = view.get("components", {})
                            available = [components.get(part, {}).get("available", False) for part in ("roster", "schedule", "standings")]
                            incomplete = any(components.get(part, {}).get("status") != "fresh" for part in ("roster", "schedule", "standings", "portraits"))
                            status = "ready" if all(available) and not incomplete else "partial" if any(available) else "failed"
                            row = {"status": status, "counts": counts, "sources": view.get("sources", []),
                                   "message": f"已收录 {counts['players']} 名球员、{counts['photos']} 张照片。" if players else
                                              "球队资料来源暂未返回有效名单，已安排重试。"}
            self.store.set_meta(key, {**row, "updated_at": now()})
        except asyncio.CancelledError:
            self.store.set_meta(key, {**self.store.get_meta(key, {}), "status": "interrupted", "updated_at": now(),
                                      "message": "资料准备已中断，已有结果保留。"})
            raise
        except Exception:
            self.store.set_meta(key, {**self.store.get_meta(key, {}), "status": "failed", "updated_at": now(),
                                      "message": "来源准备暂未完成，稍后会重试。"})
        finally:
            self.tasks.pop((kind, team_id), None)

    async def close(self):
        active = list(self.tasks.values())
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        # A task cancelled before its first await cannot run its own finalizer.
        for team in recent_teams(self.store):
            for kind in ("team", "news"):
                key = self._key(kind, team)
                row = self.store.get_meta(key, {})
                if row.get("status") == "running":
                    self.store.set_meta(key, {**row, "status": "interrupted", "updated_at": now()})
        self.tasks.clear()

    async def stop(self, kind, team_id):
        key = self._key(kind, team_id)
        active = self.tasks.get((kind, team_id))
        if active and not active.done():
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
            self.store.set_meta(key, {**self.store.get_meta(key, {}), "status": "interrupted", "updated_at": now(),
                                      "message": "资料准备已停止，已有结果保留。"})
        return self.status(kind, team_id)
