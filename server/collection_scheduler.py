"""Resident data-only upkeep of followed and recently visited clubs."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from .task_memory import state_key


class CollectionScheduler:
    INTERVAL_SECONDS = 60
    REVIEW_SECONDS = 1800
    TEAM_TIMEOUT_SECONDS = 180
    BATCH_SIZE = 2

    def __init__(self, store, team_maintenance, *, enabled=True, data_preparation=None):
        self.store, self.maintenance = store, team_maintenance
        self.enabled = enabled
        self.data_preparation = data_preparation
        self.task = None
        self.lock = asyncio.Lock()

    def status(self):
        return {**self.store.get_meta("collection_scheduler", {"teams": {}}),
                "enabled": self.enabled, "running": bool(self.task and not self.task.done())}

    def start(self, *, delay=30):
        if not self.enabled or self.task and not self.task.done():
            return False
        self.task = asyncio.create_task(self._loop(delay))
        return True

    async def _loop(self, delay):
        await asyncio.sleep(delay)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A corrupt/missing team should not terminate the resident loop.
                status = self.store.get_meta("collection_scheduler", {"teams": {}})
                self.store.set_meta("collection_scheduler", {**status, "last_error": "本轮维护未完成，稍后重试。"})
            await asyncio.sleep(self.INTERVAL_SECONDS)

    async def run_once(self, *, now=None):
        if not self.enabled or self.lock.locked():
            return {"started": False}
        async with self.lock:
            moment = now or datetime.now(timezone.utc)
            status = self.store.get_meta("collection_scheduler", {"teams": {}})
            states = status.setdefault("teams", {})
            followed = list(dict.fromkeys(team for team in self.store.settings().get("followed_teams", [])
                                          if self.store.get("teams", team)))
            from .club_preparation import recent_teams
            followed = list(dict.fromkeys([*followed, *recent_teams(self.store, moment=moment)]))
            cursor = int(status.get("cursor", 0)) % max(1, len(followed))
            ordered = followed[cursor:] + followed[:cursor]
            processed = []
            for team_id in ordered:
                previous = states.get(team_id, {})
                try:
                    next_check = datetime.fromisoformat(previous.get("next_check", ""))
                    if next_check.tzinfo and next_check > moment:
                        continue
                except (TypeError, ValueError):
                    pass
                manual = self.store.get_meta(state_key("team", team_id), {})
                if manual.get("status") in {"running", "waiting", "stopping"}:
                    continue
                processed.append(team_id)
                if self.data_preparation:
                    news = self.data_preparation.status("news", team_id)
                    if news["status"] not in {"idle", "interrupted"}:
                        self.data_preparation.start("news", team_id, remember=False)
                status["cursor"] = (followed.index(team_id) + 1) % max(1, len(followed))
                try:
                    async with asyncio.timeout(self.TEAM_TIMEOUT_SECONDS):
                        result = await self.maintenance.review_team(team_id, budget=12, now=moment, trigger="automatic")
                        if result.get("started"):
                            result = await self.maintenance.wait_for_team(team_id)
                    failed = result.get("status") in {"failed", "partial", "interrupted", "unavailable"}
                    photo = (result.get("components") or {}).get("portraits") or {}
                    continuing_photos = result.get("status") == "partial" and photo.get("continuation_ready") is True
                    if continuing_photos:
                        failed = False
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failed, continuing_photos, result = True, False, {"status": "failed"}
                failures = min(10, int(previous.get("failures", 0)) + 1) if failed else 0
                wait = min(3600, 300 * 2 ** (failures - 1)) if failed else self.REVIEW_SECONDS
                if continuing_photos:
                    wait = self.INTERVAL_SECONDS
                if result.get("status") == "running":
                    wait = self.INTERVAL_SECONDS
                states[team_id] = {"last_check": moment.isoformat(), "next_check": (moment + timedelta(seconds=wait)).isoformat(),
                                   "status": result.get("status", "checked"), "failures": failures,
                                   "last_error": "来源暂不可用，已保留资料并安排重试。" if failed else None}
                status.update(last_check=moment.isoformat(), last_error=None)
                self.store.set_meta("collection_scheduler", status)
                if len(processed) >= self.BATCH_SIZE:
                    break
            status["next_check"] = (moment + timedelta(seconds=self.INTERVAL_SECONDS)).isoformat()
            self.store.set_meta("collection_scheduler", status)
            return {"started": True, "teams": processed}

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
