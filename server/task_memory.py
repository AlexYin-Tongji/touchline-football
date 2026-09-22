"""Progress memory is not football evidence or a model's private transcript."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


def state_key(kind, team_id):
    return f"workflow:{kind}:{team_id}"


@lru_cache(maxsize=1)
def system_mission():
    return (Path(__file__).resolve().parents[1] / "agent_prompts/system.md").read_text(encoding="utf-8").strip()


def progress_context(store, kind, team_id):
    state = store.get_meta(state_key(kind, team_id), {})
    memory = state.get("memory", {})
    # Only machine-maintained counts and task directions, never source titles,
    # raw articles, previous model answers, credentials or arbitrary metadata.
    return {"功能": "球队动态" if kind == "news" else "球队信息", "球队": team_id,
            "历史完成数": max(0, int(memory.get("completed_count", 0))),
            "本轮完成数": max(0, int(state.get("progress", {}).get("completed", 0))),
            "本轮待处理数": max(0, int(state.get("progress", {}).get("remaining", 0))),
            "说明": "这些是工作进度，不是足球事实；只完成当前任务，已保存的成果不要重复发布。引用仍须读取当前有效来源。"}


def prompt_context(store, kind, team_id):
    return json.dumps(progress_context(store, kind, team_id), ensure_ascii=False)
