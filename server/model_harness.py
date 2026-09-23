"""One runtime dispatcher for bounded, observable requests across model roles."""
from __future__ import annotations

import asyncio
import copy
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import time
import uuid

from .storage import provider_origin


@dataclass
class _Request:
    id: str
    root_id: str
    origin: tuple
    ready: asyncio.Future
    granted: bool = False


class ModelHarness:
    def __init__(self, global_limit=6, provider_limit=4):
        if global_limit < 1 or provider_limit < 1:
            raise ValueError("Model request limits must be positive")
        self.global_limit = global_limit
        self.provider_limit = provider_limit
        self.active = {}
        self.recent = deque(maxlen=100)
        self._running = 0
        self._providers = {}
        self._pending = {}
        self._roots = deque()

    def _dispatch(self):
        """Round-robin roots; blocked origins never reserve execution slots."""
        while self._running < self.global_limit and self._roots:
            selected = None
            for _ in range(len(self._roots)):
                root_id = self._roots.popleft()
                queue = self._pending[root_id]
                selected = next((item for item in queue if not item.ready.cancelled()
                                 and self._providers.get(item.origin, 0) < self.provider_limit), None)
                if selected is not None:
                    queue.remove(selected)
                if queue:
                    self._roots.append(root_id)
                else:
                    del self._pending[root_id]
                if selected is not None:
                    break
            if selected is None:
                return
            selected.granted = True
            self._running += 1
            self._providers[selected.origin] = self._providers.get(selected.origin, 0) + 1
            selected.ready.set_result(None)

    def _release(self, ticket):
        if ticket.granted:
            self._running -= 1
            remaining = self._providers[ticket.origin] - 1
            if remaining:
                self._providers[ticket.origin] = remaining
            else:
                del self._providers[ticket.origin]
        else:
            queue = self._pending.get(ticket.root_id)
            if queue is not None:
                queue.remove(ticket)
                if not queue:
                    del self._pending[ticket.root_id]
                    self._roots.remove(ticket.root_id)
        self._dispatch()

    async def request(self, config, execute, *, owner_id, role, stage, root_id=None):
        origin = provider_origin(config.get("base_url"))
        identity = uuid.uuid4().hex
        root_id = root_id or owner_id
        record = {"id": identity, "owner_id": owner_id, "root_id": root_id, "role": role, "stage": stage,
                  "provider_id": config.get("_provider_id"), "model": config.get("model", ""),
                  "status": "queued", "created_at": datetime.now(timezone.utc).isoformat()}
        self.active[identity] = record
        started = time.monotonic()
        executing = None
        ticket = _Request(identity, root_id, origin, asyncio.get_running_loop().create_future())
        if root_id not in self._pending:
            self._pending[root_id] = deque()
            self._roots.append(root_id)
        self._pending[root_id].append(ticket)
        try:
            self._dispatch()
            await ticket.ready
            executing = time.monotonic()
            record.update(status="running", wait_ms=round((executing - started) * 1000))
            result = await execute()
            record["status"] = "completed"
            return result
        except asyncio.CancelledError:
            record["status"] = "cancelled"
            raise
        except Exception:
            record["status"] = "failed"
            raise
        finally:
            finished = time.monotonic()
            record.setdefault("wait_ms", round((finished - started) * 1000))
            record["elapsed_ms"] = round((finished - started) * 1000)
            record["execution_ms"] = round((finished - executing) * 1000) if executing is not None else 0
            self.active.pop(identity, None)
            self.recent.append(record)
            self._release(ticket)

    def snapshot(self):
        return copy.deepcopy({"limits": {"total": self.global_limit, "per_provider": self.provider_limit},
                              "active": list(self.active.values()), "recent": list(self.recent)})


def runtime_harness(store):
    if not hasattr(store, "_model_harness"):
        store._model_harness = ModelHarness()
    return store._model_harness
