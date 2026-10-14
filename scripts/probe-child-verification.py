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
from tests.test_agent import FakeStore  # noqa: E402


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
