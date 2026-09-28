"""Personal defaults and a Chinese feed; maintenance stays behind the UI."""
from __future__ import annotations

import asyncio
import base64
import json
import re
from datetime import datetime, timezone

from . import ingestion
from .presentation import readable_chinese


def install_personal_defaults(store):
    if store.get_meta("personal_v7_defaults"):
        return
    # Model credentials, followed clubs and user-written notes are preserved.
    # Personal use has no separate demo workspace or operational setup wizard.
    existing = store.get_meta("settings", {})
    store.save_settings({"mode": "live", "spoiler_free": existing.get("spoiler_free", False),
                         "reports": {"auto_generate": True},
                         "automation": {"enabled": False}})
    store.set_meta("personal_v7_defaults", True)


def source_links(store, event, evidence_ids=None):
    result, seen = [], set()
    for identity in event.get("evidence_ids", []):
        if evidence_ids is not None and identity not in evidence_ids:
            continue
        row = store.get("evidence", identity)
        if not row or row.get("mode") != event.get("mode") or row.get("team_id") != event.get("team_id"):
            continue
        if row.get("private") or row.get("deleted") or row.get("visibility") in {"hidden", "deleted"}:
            continue
        url = row.get("url")
        if not url or url in seen:
            continue
        try:
            url = ingestion.normalize_url(url)
        except ValueError:
            continue
        seen.add(url)
        result.append({"id": identity, "url": url, "title": row.get("title", ""), "name": row.get("source_name", "来源"), "source_name": row.get("source_name", "来源"), "published_at": row.get("published_at"), **source_tier(url)})
    return result


# How each registered tier is described to a reader. The wording is what tells
# someone how much weight to give a line, so it is fixed here rather than
# composed per surface.
TIER_LABELS = {
    "official": "官方",
    "dataset": "数据源",
    "press": "媒体",
    "community": "球迷社区",
    "derived": "本站计算",
}

# A host we did not review is not evidence of anything, so it is described as
# unclassified rather than promoted to any tier by default.
UNCLASSIFIED_LABEL = "未分类来源"


def source_tier(url):
    """The reviewed tier of a source, for display next to it.

    An unregistered host returns None rather than a default. Defaulting to a
    tier would let an unreviewed blog appear alongside the club's own site.
    """
    from . import providers, provenance
    # Importing the providers is what grants their hosts a tier, so it is done
    # here rather than trusted to whether some other module happened to load
    # them. A missing import would show every source as unclassified.
    providers.load()
    try:
        tier = provenance.tier_for_url(url)
    except Exception:
        tier = None
    if tier is None:
        return {"tier": None, "tier_label": UNCLASSIFIED_LABEL, "tier_known": False}
    return {"tier": tier.value, "tier_label": TIER_LABELS.get(tier.value, UNCLASSIFIED_LABEL), "tier_known": True}


def safe_images(rows):
    output, seen = [], set()
    for image in rows:
        if isinstance(image, str):
            image = {"url": image}
        if not isinstance(image, dict):
            continue
        url = image.get("url") or image.get("src")
        url = ingestion.safe_image_url(url, image.get("source_url") or "")
        if not url or ingestion.image_identity(url) in seen:
            continue
        seen.add(ingestion.image_identity(url))
        output.append({**image, "url": url, "src": url})
    return output[:4]


def message_topic(title):
    """Classify the actual Chinese headline, not incidental words in old RSS."""
    if re.search(r"转会|加盟|签约|续约|租借|引援|报价|离队|转投", title):
        return "transfer"
    if re.search(r"\d+\s*(?:[-–:：]|比)\s*\d+|赛后|战报|击败|战胜|大胜|完胜|逆转|绝杀|对阵|前瞻|帽子戏法|梅开二度", title):
        return "match"
    if re.search(r"伤病|受伤|因伤|伤情|伤停|复出|康复|缺阵|手术|恢复训练", title):
        return "injury"
    return "team"


def feed_view(store, team_id=None, cursor=None, limit=20, *, topic=None):
    from .news_reports import list_reports, queue_summary
    settings = store.settings()
    teams = {team_id} if team_id else set(settings.get("followed_teams", []))
    reports = [r for r in list_reports(store, team_id=team_id, mode="live") if r.get("team_id") in teams]
    by_event = {row["event_id"]: row for row in sorted(reports, key=lambda r: r.get("updated_at", ""))}
    items, pending_events = [], set()
    for event in store.list("events", "live", team_id):
        if event.get("team_id") not in teams:
            continue
        visible = []
        for eid in event.get("evidence_ids", []):
            source = store.get("evidence", eid)
            if (not source or source.get("mode") != "live" or source.get("team_id") != event["team_id"]
                    or source.get("private") or source.get("deleted") or source.get("visibility") in {"hidden", "deleted"}
                    or source.get("fixture_record")):
                continue
            view = ingestion.safe_evidence_view(source) if settings.get("spoiler_free", True) else source
            if view:
                visible.append(view)
        if not visible:
            continue
        report = by_event.get(event["id"])
        content = report.get("report") if report else None
        ready = bool(report and report.get("status") == "completed" and isinstance(content, dict)
                     and readable_chinese(content.get("title", "")) and readable_chinese(content.get("lead", "")))
        content = content if ready else {}
        chinese_source = next((source for source in visible if readable_chinese(source.get("title", "")) and readable_chinese(source.get("text", ""))), None)
        # A vanished/private source or expired original cannot revive an old
        # event excerpt. Persisted Chinese AI reports have their own provenance.
        if not ready and not chinese_source:
            pending_events.add(event["id"])
            continue
        title = content.get("title") or (chinese_source or {}).get("title", "")
        summary = content.get("lead") or (chinese_source or {}).get("text", "")[:300]
        if not readable_chinese(title) or not readable_chinese(summary):
            pending_events.add(event["id"])
            continue
        # Guard the feed from the previous version's empty announcement cards.
        if "相关报道已收录" in summary or "请助手整理中文要点" in summary:
            pending_events.add(event["id"])
            continue
        # Only evidence that actually supports this displayed Chinese message
        # may contribute its attribution or photographs. Event membership is
        # broader and can include older or unrelated linked source captures.
        cited_ids = {source.get("id") for source in content.get("sources", [])} if ready else {chinese_source["id"]}
        visible_ids = {source["id"] for source in visible} & cited_ids
        sources = source_links(store, event, evidence_ids=visible_ids)
        if not sources and event.get("mode") != "live":
            continue
        images = [image for source in visible if source["id"] in visible_ids
                  for image in ingestion.source_images(source)]
        published = max((source.get("published_at") or "" for source in sources), default="") or None
        items.append({"id": event["id"], "team_id": event["team_id"], "title": title, "summary": summary,
                      "topic": message_topic(title), "published_at": published, "updated_at": published or event.get("updated_at") or (report or {}).get("updated_at"),
                      "images": safe_images(images), "sources": sources,
                      "source_gap_count": len(content.get("source_gaps") or []),
                      "report_id": report["id"] if ready else None, "status": "ready" if ready else "source",
                      "evidence_ids": [s["id"] for s in sources]})
    if topic:
        items = [row for row in items if row["topic"] == topic]
    items.sort(key=lambda row: (row.get("updated_at") or "", row["id"]), reverse=True)
    if cursor:
        try:
            boundary = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if not isinstance(boundary, list) or len(boundary) != 2 or not all(isinstance(v, str) for v in boundary):
                raise ValueError()
        except (ValueError, TypeError, UnicodeError):
            raise ingestion.IngestionError("动态列表已更新，请刷新后继续浏览。") from None
        items = [row for row in items if (row.get("updated_at") or "", row["id"]) < tuple(boundary)]
    visible = items[:limit]
    next_cursor = (base64.urlsafe_b64encode(json.dumps([visible[-1].get("updated_at") or "", visible[-1]["id"]], separators=(",", ":")).encode()).decode().rstrip("=")
                   if len(items) > limit and visible else None)
    statuses = [store.get_meta("personal_refresh_" + identity, {}) for identity in teams]
    pending_reports = [{**report, "status": "stale"} if report.get("status") == "completed" else report
                       for report in by_event.values() if report["event_id"] in pending_events]
    processing = queue_summary(store, reports=pending_reports,
                               waiting_sources=len(pending_events - {report["event_id"] for report in pending_reports}))
    return {"items": visible, "next_cursor": next_cursor, "pending_count": len(pending_events), "processing": processing,
            "refresh": {"status": "running" if any(s.get("status") == "running" for s in statuses) else "idle",
                        "last_updated": max((s.get("last_updated") or "" for s in statuses), default="") or None,
                        "error": next((s["error"] for s in statuses if s.get("error")), None)}}


class PersonalRefreshManager:
    def __init__(self, store, team_maintenance):
        self.store, self.team_maintenance = store, team_maintenance
        self.tasks = {}
        self.last_selected = None

    def start(self, team_id=None, *, force=False):
        targets = [team_id] if team_id else sorted(self.store.settings().get("followed_teams", []), key=lambda identity: self.store.get_meta("personal_refresh_" + identity, {}).get("last_attempt", ""))
        started = False
        for identity in targets:
            if not self.store.get("teams", identity) or identity in self.tasks or len(self.tasks) >= 2:
                continue
            status = self.store.get_meta("personal_refresh_" + identity, {})
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(status["last_attempt"])).total_seconds()
            except (KeyError, ValueError, TypeError):
                age = float("inf")
            if not force and age < 3600:
                continue
            self.store.set_meta("personal_refresh_" + identity, {**status, "status": "running", "last_attempt": ingestion.now(), "error": None})
            self.tasks[identity] = asyncio.create_task(self._run(identity, force=force))
            started = True
        return {"started": started, "status": "running" if self.tasks else "idle"}

    async def _run(self, team_id, *, force=False):
        from .research import collect_team
        from .news_reports import enqueue_reports
        key = "personal_refresh_" + team_id
        try:
            self.team_maintenance.start(team_id)
            result = await asyncio.wait_for(collect_team(self.store, team_id, query="", limit=10, max_age_days=7, force=force), timeout=180)
            enqueue_reports(self.store, team_id=team_id)
            errors = result.get("errors", [])
            self.store.set_meta(key, {**self.store.get_meta(key, {}), "status": "idle", "last_updated": ingestion.now(),
                                      "coverage": result.get("coverage"), "source_errors": errors,
                                      "collected_count": len(result.get("evidence_ids", [])),
                                      "error": "部分消息来源暂时不可用，已保留成功取得的资料。" if errors and result.get("evidence_ids") else
                                               "消息来源暂时不可用，可刷新来源后重试。" if errors else None})
        except asyncio.CancelledError:
            self.store.set_meta(key, {**self.store.get_meta(key, {}), "status": "idle"})
            raise
        except Exception:
            self.store.set_meta(key, {**self.store.get_meta(key, {}), "status": "idle", "error": "消息更新暂未完成，可手动重试。"})
        finally:
            self.tasks.pop(team_id, None)

    async def close(self):
        identities = list(self.tasks)
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for identity in identities:
            key = "personal_refresh_" + identity
            status = self.store.get_meta(key, {})
            if status.get("status") == "running":
                self.store.set_meta(key, {**status, "status": "idle"})
        self.tasks.clear()
