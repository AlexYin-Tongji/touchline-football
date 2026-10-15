"""Does an evidence-free child still deliver with verification switched on?

The plan flagged this as the thing to check before opening child verification:
`submit_answer` refuses `status=complete` without facts, and every fact needs an
evidence id read *this round*. A maintenance block (a roster sync, a standings
refresh) reads no article, so the question is whether turning verification on
for children would make those children unable to deliver at all.

Read-only probe against the real LocalTools/submit_answer path with synthetic
data. Writes nothing.
"""
from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.agent_tools import LocalTools, ToolError  # noqa: E402


class FakeStore:
    """Just enough store for this probe: one team, one run, no metadata.

    Kept here rather than borrowed from ``tests/`` so this script stays runnable
    on its own.
    """

    def __init__(self):
        self.data = {name: {} for name in ("teams", "runs")}
        self.config = {"spoiler_free": True, "explanation_depth": "standard",
                       "llm": {"base_url": "http://localhost:9999/v1", "model": "test",
                               "api_key": "secret-example", "max_rounds": 5, "timeout_seconds": 10}}
        self.put("teams", {"id": "arsenal", "name": "阿森纳", "english_name": "Arsenal"})

    def put(self, collection, item):
        self.data.setdefault(collection, {})[item["id"]] = copy.deepcopy(item)
        return copy.deepcopy(item)

    def get(self, collection, item_id):
        return copy.deepcopy(self.data.get(collection, {}).get(item_id))

    def list(self, collection, mode=None, team_id=None):
        return [copy.deepcopy(item) for item in self.data.get(collection, {}).values()
                if (mode is None or item.get("mode") == mode)
                and (team_id is None or item.get("team_id") == team_id)]

    def settings(self):
        return copy.deepcopy(self.config)

    def get_meta(self, key, default=None):
        return copy.deepcopy(self.data.get("metadata", {}).get(key, default))

    def set_meta(self, key, value):
        self.data.setdefault("metadata", {})[key] = copy.deepcopy(value)
        return value

    def atomic(self):
        from contextlib import nullcontext
        return nullcontext()


def child_run(**patches):
    base = {"id": "agent_child", "mode": "live", "team_id": "arsenal", "skill": "team_refresh",
            "question": "同步阵容", "conversation_id": "agent_child", "status": "queued",
            "answer": "", "evidence_ids": [], "warnings": [], "steps": [], "error": None,
            "created_at": "2026-10-13T00:00:00Z", "finished_at": None, "model": "",
            "internal_agent": True, "scope_skill": "team_refresh",
            "tool_allowlist": ["get_team_data", "maintain_team_data", "read_evidence", "submit_answer"]}
    base.update(patches)
    return base


def attempt(label, settings, run):
    store = FakeStore()
    store.put("runs", run)
    local = LocalTools(store, run, settings, None)
    print(f"--- {label}")
    print(f"    verification_enabled = {local.verification_enabled}")
    # A maintenance child reports an operation, not a quotation: no facts.
    payload = {"status": "complete", "answer": "阵容已同步，7 名球员信息更新。", "facts": []}
    try:
        result = local.call("submit_answer", payload)
        print(f"    DELIVERED: {result.get('submitted')} facts={result.get('facts_count')}")
    except ToolError as exc:
        print(f"    REFUSED: {exc}")


def main():
    on = {"spoiler_free": True, "explanation_depth": "standard", "verification_enabled": True,
          "llm": {"base_url": "http://localhost:9999/v1", "model": "test", "api_key": "k", "timeout_seconds": 10}}
    off = {**on, "verification_enabled": False}

    attempt("maintenance child, verification OFF (today's behaviour)", off, child_run())
    attempt("maintenance child, verification ON (proposed 1.5)", on, child_run())
    attempt("question child, verification ON, no read", on,
            child_run(skill="ask", scope_skill="ask", question="伤病确认了吗？"))


if __name__ == "__main__":
    main()
