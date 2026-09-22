"""One runtime dispatcher for bounded, observable requests across model roles."""
from __future__ import annotations

import asyncio
import copy
from collections import deque
from datetime import datetime, timezone
import time
import uuid

from .storage import provider_origin


class ModelHarness:
    def __init__(self, global_limit=3, provider_limit=2):
        self.global_limit = global_limit
        self.provider_limit = provider_limit
        self.gate = asyncio.Semaphore(global_limit)
        self.providers = {}
        self.active = {}
        self.recent = deque(maxlen=100)

    async def request(self, config, execute, *, owner_id, role, stage):
        identity = uuid.uuid4().hex
        record = {"id": identity, "owner_id": owner_id, "role": role, "stage": stage,
                  "provider_id": config.get("_provider_id"), "model": config.get("model", ""),
                  "status": "queued", "created_at": datetime.now(timezone.utc).isoformat()}
        self.active[identity] = record
        started = time.monotonic()
        provider_gate = self.providers.setdefault(provider_origin(config.get("base_url")), asyncio.Semaphore(self.provider_limit))
        try:
            # A busy provider must not occupy all global slots while waiting.
            async with provider_gate, self.gate:
                record.update(status="running", wait_ms=round((time.monotonic() - started) * 1000))
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
            record["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            self.active.pop(identity, None)
            self.recent.append(record)

    def snapshot(self):
        return copy.deepcopy({"limits": {"total": self.global_limit, "per_provider": self.provider_limit},
                              "active": list(self.active.values()), "recent": list(self.recent)})


def runtime_harness(store):
    if not hasattr(store, "_model_harness"):
        store._model_harness = ModelHarness()
    return store._model_harness
