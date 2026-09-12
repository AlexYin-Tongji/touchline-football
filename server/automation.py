"""A bounded local update cycle that reuses existing collection and agent runs."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from .ingestion import safe_evidence_view


def now():
    return datetime.now(timezone.utc).isoformat()


class AutomationManager:
    def __init__(self, store, start_run):
        self.store, self.start_run = store, start_run
        self.task = None

    def status(self):
        status = self.store.get_meta("automation_status", {})
        status["running"] = bool(self.task and not self.task.done())
        config = self.store.settings().get("automation", {})
        if config.get("enabled") and status.get("last_checked"):
            status["next_check"] = (datetime.fromisoformat(status["last_checked"]) + timedelta(minutes=config.get("interval_minutes", 60))).isoformat()
        elif config.get("enabled"):
            status["next_check"] = now()
        else:
            status["next_check"] = None
        return status

    def due(self):
        if not self.store.settings().get("automation", {}).get("enabled") or self.status()["running"]:
            return False
        checked = self.status().get("last_checked")
        if not checked:
            return True
        try:
            return datetime.now(timezone.utc) - datetime.fromisoformat(checked) >= timedelta(minutes=self.store.settings()["automation"]["interval_minutes"])
        except (ValueError, TypeError):
            return True

    def start(self, team_id=None):
        if self.task and not self.task.done():
            return {"started": False, "status": self.status()}
        self.task = asyncio.create_task(self.run_once(team_id))
        return {"started": True, "status": self.status()}

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def run_once(self, team_id=None):
        from . import fixtures, research
        from .recommendations import get_recommendations
        settings = self.store.settings()
        config = settings.get("automation", {})
        followed = settings.get("followed_teams", [])
        # Rotate the bounded work window so clubs after the fourth are not
        # permanently starved as the followed list grows across five leagues.
        offset = self.store.get_meta("automation_team_cursor", 0) % max(1, len(followed))
        teams = [team_id] if team_id else (followed[offset:] + followed[:offset])[:4]
        if not team_id and followed:
            self.store.set_meta("automation_team_cursor", (offset + len(teams)) % len(followed))
        details, errors, last_run = [], [], None
        self.store.set_meta("automation_status", {**self.status(), "stage": "正在更新球队资料", "last_error": None})
        try:
            for current_team in teams:
                if not self.store.get("teams", current_team):
                    continue
                if config.get("sync_matches", True):
                    synced = await asyncio.wait_for(fixtures.sync_team(self.store, current_team), timeout=150)
                    errors.extend(synced.get("errors", [])[:4])
                collected = await asyncio.wait_for(research.collect_team(self.store, current_team, query="球队最新消息", limit=4), timeout=150)
                errors.extend(collected.get("errors", [])[:4])
                records = [item for item in self.store.list("evidence", "live", current_team)
                    if not settings.get("spoiler_free", True) or safe_evidence_view(item) is not None]
                ids = {item["id"] for item in records}
                baseline = set(self.store.get_meta("automation_baseline_" + current_team, []))
                changed = sorted(ids - baseline)
                previous_id = self.store.get_meta("automation_last_run_" + current_team)
                previous = self.store.get("runs", previous_id) if previous_id else None
                retry = previous and previous.get("status") == "failed"
                is_active = previous and previous.get("status") in {"queued", "running"}
                needs_first_brief = not previous and not any(run.get("skill") == "team_brief" and run.get("status") == "completed"
                    for run in self.store.list("runs", "live", current_team))
                if config.get("generate_brief") and (changed or retry or needs_first_brief) and not is_active and settings["llm"].get("model") and settings["llm"].get("base_url"):
                    response = await self.start_run({"mode": "live", "team_id": current_team, "skill": "team_brief",
                        "question": "基于已经更新的原始资料，最多两条值得知道的新变化。附准确原文片段并核对关键结论，区分来源说法与推测。", "allow_network": False})
                    last_run = response["id"]
                    self.store.set_meta("automation_last_run_" + current_team, last_run)
                self.store.set_meta("automation_baseline_" + current_team, sorted(ids)[-1000:])
                details.append({"team_id": current_team, "new_evidence": len(changed), "collected": collected.get("added", 0)})
            get_recommendations(self.store, "live")
            self.store.set_meta("automation_status", {"last_checked": now(), "last_error": "；".join(errors[:6]) or None,
                "last_run_id": last_run or self.status().get("last_run_id"), "teams": details,
                "stage": "资料已更新" if not errors else "更新完成，部分来源需要检查"})
        except asyncio.CancelledError:
            self.store.set_meta("automation_status", {**self.status(), "stage": "更新已停止", "last_checked": now(), "last_error": "本次更新已停止。"})
        except Exception:
            self.store.set_meta("automation_status", {**self.status(), "stage": "更新未完成", "last_checked": now(), "last_error": "本次自动更新未完成，请检查来源或稍后重试。", "teams": details})
