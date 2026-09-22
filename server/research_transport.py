"""Per-store in-flight sharing; no successful response or article-body cache."""
from __future__ import annotations

import asyncio
import time

from .ingestion import IngestionError

REQUEST_CONCURRENCY = 4
HOST_CONCURRENCY = 2
FAILURE_COOLDOWN_SECONDS = 30
MAX_FAILURES = 256


class RequestCoordinator:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.pending = {}
        self.failures = {}
        self.capacity = asyncio.Semaphore(REQUEST_CONCURRENCY)
        self.hosts = {}

    async def request(self, key, host, factory, *, force=False):
        now = time.monotonic()
        self.failures = {key: value for key, value in self.failures.items() if value[0] > now}
        if force:
            self.failures.pop(key, None)
        elif key in self.failures:
            raise IngestionError(self.failures[key][1])
        entry = self.pending.get(key)
        if entry is None:
            async def perform():
                try:
                    # Acquire the host slot first, so a busy publisher cannot
                    # occupy all global slots while other publishers are ready.
                    async with self.hosts.setdefault(host, asyncio.Semaphore(HOST_CONCURRENCY)):
                        async with self.capacity:
                            return await factory()
                except (IngestionError, asyncio.TimeoutError) as exc:
                    message = str(exc) or "来源请求超时，请稍后重试"
                    self.failures[key] = (time.monotonic() + FAILURE_COOLDOWN_SECONDS, message)
                    while len(self.failures) > MAX_FAILURES:
                        self.failures.pop(next(iter(self.failures)))
                    raise IngestionError(message) from None
            entry = {"task": asyncio.create_task(perform()), "waiters": 0}
            self.pending[key] = entry
        entry["waiters"] += 1
        try:
            # One cancelled reader must not interrupt another active reader.
            return await asyncio.shield(entry["task"])
        finally:
            entry["waiters"] -= 1
            if not entry["waiters"]:
                self.pending.pop(key, None)
                task = entry["task"]
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def shared_request(store, key, host, factory, *, force=False):
    state = getattr(store, "_research_requests", None)
    if state is None or state.loop is not asyncio.get_running_loop():
        state = RequestCoordinator()
        store._research_requests = state
    return await state.request(key, host, factory, force=force)
