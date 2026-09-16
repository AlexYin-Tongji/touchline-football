"""Durable conversation views over saved turns, independent of the dashboard limit."""
from __future__ import annotations

import base64
import json

from fastapi import HTTPException


def conversation_runs(store, identifier=None, *, team_id=None, mode=None):
    rows = [row for row in store.list("runs", mode, team_id)
            if not row.get("internal_report") and not row.get("report_id") and not row.get("tracking_id")
            and (identifier is None or (row.get("conversation_id") or row["id"]) == identifier)]
    return sorted(rows, key=lambda row: (row.get("created_at", ""), row["id"]))


def conversation_binding(store, rows, *, strict=False):
    explicit = next((row for row in rows if row.get("provider_id")), None)
    if explicit:
        return {"provider_id": explicit["provider_id"], "model": explicit.get("model")}
    legacy = next((row for row in rows if row.get("model") and "未调用" not in row["model"] and "演示" not in row["model"]), None)
    profiles = store.list("model_profiles")
    if not legacy or not profiles:
        return None
    candidates = [profile for profile in profiles if any(item["id"] == legacy["model"] for item in profile.get("models", []))
                  and (not legacy.get("protocol") or profile.get("protocol") == legacy["protocol"])]
    if len(candidates) == 1:
        return {"provider_id": candidates[0]["id"], "model": legacy["model"]}
    if strict:
        raise HTTPException(409, "旧对话的模型服务无法唯一确认，请保留历史并新建对话选择模型。")
    return None


def summarize(rows, store=None):
    first, latest = rows[0], rows[-1]
    result = {"id": first.get("conversation_id") or first["id"], "team_id": first["team_id"], "mode": first["mode"],
            "title": first.get("question", "未命名对话")[:200], "created_at": first.get("created_at"),
            "updated_at": latest.get("finished_at") or latest.get("created_at"), "run_count": len(rows),
            "last_status": latest.get("status"), "provider_id": first.get("provider_id"),
            "model": first.get("model"), "model_role": first.get("model_role", "chat"),
            "match_id": latest.get("match_id")}
    if store is not None and first["mode"] == "live":
        binding = conversation_binding(store, rows)
        if binding:
            result.update(binding)
    return result


def list_conversations(store, *, team_id=None, mode="live", cursor=None, limit=30):
    groups = {}
    for row in conversation_runs(store, team_id=team_id, mode=mode):
        groups.setdefault(row.get("conversation_id") or row["id"], []).append(row)
    rows = sorted((summarize(group, store) for group in groups.values()),
                  key=lambda row: (row.get("updated_at") or "", row["id"]), reverse=True)
    if cursor:
        try:
            boundary = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if not isinstance(boundary, list) or len(boundary) != 2 or not all(isinstance(v, str) for v in boundary):
                raise ValueError()
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(400, "对话列表已更新，请重新打开。") from None
        rows = [row for row in rows if (row.get("updated_at") or "", row["id"]) < tuple(boundary)]
    visible = rows[:limit]
    next_cursor = None
    if len(rows) > limit and visible:
        last = visible[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps([last.get("updated_at") or "", last["id"]]).encode()).decode().rstrip("=")
    return {"conversations": visible, "next_cursor": next_cursor}
