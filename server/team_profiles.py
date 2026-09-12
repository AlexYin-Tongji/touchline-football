"""Persistent club backgrounds and source-linked notes from completed answers.

Club background is deliberately separate from dated news. Archived answer facts
are retrieval leads, never a replacement for reading their original evidence.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Literal

from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .ingestion import normalize_url


from .club_library import CLUB_PROFILES, PROFILE_REVIEWED_AT


def seed_team_profiles(store):
    """Add missing fields only: an upgrade must not replace personal edits."""
    for team_id, profile in CLUB_PROFILES.items():
        team = store.get("teams", team_id)
        if not team:
            continue
        additions = {key: value for key, value in profile.items() if key not in team}
        if additions:
            team.update(additions)
            team.setdefault("profile_reviewed_at", PROFILE_REVIEWED_AT)
            store.put("teams", team)


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def _normalized(text):
    return " ".join(str(text or "").split())


def _evidence_hash(evidence):
    # Include metadata as well as text so a corrected source is not silently
    # treated as support for an older extracted claim.
    value = {key: evidence.get(key) for key in (
        "text", "title", "url", "source_name", "published_at", "fetched_at", "origin", "spoiler")}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def archive_run_knowledge(store, run, *, read_cache=None):
    """Archive delivered facts with valid read provenance, never analysis.

Unverified deliveries retain the weaker ``citation_checked`` label. When an
independent support report exists, only its supported fact text may be saved.
"""
    if run.get("status") != "completed" or run.get("mode") not in {"live", "demo"}:
        return []
    if not store.get("teams", run.get("team_id")) or not read_cache:
        return []
    read_ids = set(run.get("read_evidence_ids") or [])
    delivered_ids = set(run.get("evidence_ids") or [])
    verification = run.get("verification") or {}
    checked = verification.get("status") not in {None, "not_requested"}
    supported = { _normalized(claim.get("text")) for claim in verification.get("claims", [])
                 if str(claim.get("id", "")).startswith("fact_") and claim.get("verdict") == "supported"}
    saved = []
    for fact in (run.get("structured_answer") or {}).get("facts", []):
        title, detail = fact.get("title", ""), fact.get("detail", "")
        evidence_ids = sorted(set(fact.get("evidence_ids") or []))
        if not title or not detail or not evidence_ids or not set(evidence_ids).issubset(read_ids & delivered_ids):
            continue
        if checked and _normalized(title + "：" + detail) not in supported:
            continue
        sources = [store.get("evidence", eid) for eid in evidence_ids]
        if any(not source or source.get("team_id") != run["team_id"] or source.get("mode") != run["mode"]
               or not source.get("text") for source in sources):
            continue
        snapshots_match = True
        for source in sources:
            snapshot = read_cache.get(source["id"])
            if not snapshot:
                snapshots_match = False
                break
            visible = source
            if snapshot.get("spoiler_redacted"):
                from .ingestion import safe_evidence_view
                visible = safe_evidence_view(source)
            if not visible or snapshot.get("text") != visible.get("text", "")[:12000] or any(
                    snapshot.get(key) != visible.get(key) for key in
                    ("title", "url", "source_name", "published_at", "fetched_at", "origin")):
                snapshots_match = False
                break
        if not snapshots_match:
            continue
        identity = json.dumps([run["mode"], run["team_id"], _normalized(detail), evidence_ids], ensure_ascii=False)
        identifier = "knowledge_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        with store.atomic() if hasattr(store, "atomic") else nullcontext():
            existing = store.get("knowledge", identifier) or {}
            source_rows = [{key: source.get(key) for key in
                            ("id", "title", "url", "source_name", "published_at", "fetched_at", "origin")}
                           | {"content_hash": _evidence_hash(source)} for source in sources]
            # A citation-only rerun cannot promote its wording to independently
            # supported merely because an old source version passed a check.
            same_sources = existing.get("sources") == source_rows
            support = "source_supported" if checked else "citation_checked"
            if same_sources and existing.get("title") == title and existing.get("support") == "source_supported":
                support = "source_supported"
            item = {"id": identifier, "mode": run["mode"], "team_id": run["team_id"],
                    "title": title, "detail": detail, "evidence_ids": evidence_ids, "sources": source_rows,
                    "support": support, "origin": "assistant", "created_at": existing.get("created_at") or _timestamp(),
                    "updated_at": run.get("finished_at") or _timestamp(),
                    "run_ids": list(dict.fromkeys([*(existing.get("run_ids") or []), run["id"]]))[-30:],
                    "spoiler": bool(run.get("spoiler")) or any(source.get("spoiler") for source in sources)}
            store.put("knowledge", item)
        saved.append(identifier)
    return saved


def knowledge_for_team(store, team_id, mode="live", *, spoiler_free=True, allowed_evidence_ids=None, limit=20):
    """Return current, scoped retrieval leads; callers must reread evidence."""
    allowed = set(allowed_evidence_ids) if allowed_evidence_ids is not None else None
    result = []
    for item in store.list("knowledge", mode=mode, team_id=team_id):
        if item.get("team_id") != team_id or item.get("mode") != mode:
            continue
        ids = item.get("evidence_ids") or []
        if not ids or (allowed is not None and not set(ids).issubset(allowed)):
            continue
        sources = [store.get("evidence", eid) for eid in ids]
        if any(not source or source.get("mode") != mode or source.get("team_id") != team_id for source in sources):
            continue
        if spoiler_free and (item.get("spoiler") or any(source.get("spoiler") for source in sources)):
            continue
        hashes = {source["id"]: source.get("content_hash") for source in item.get("sources", [])}
        if any(hashes.get(source["id"]) != _evidence_hash(source) for source in sources):
            continue
        result.append(item)
    result.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return result if limit is None else result[:max(0, min(100, limit))]


class ProfileSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=8, max_length=4096)

    @field_validator("url")
    @classmethod
    def safe_url(cls, value):
        return normalize_url(value)


class TeamProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    country: str | None = Field(None, max_length=80)
    city: str | None = Field(None, max_length=100)
    founded: int | None = Field(None, ge=1800, le=2100)
    stadium: str | None = Field(None, max_length=100)
    nickname: str | None = Field(None, max_length=100)
    league_name: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=1500)
    official_url: str | None = Field(None, max_length=4096)
    crest_url: str | None = Field(None, max_length=4096)
    crest_source_url: str | None = Field(None, max_length=4096)
    crest_attribution: str | None = Field(None, max_length=300)
    profile_sources: list[ProfileSource] | None = Field(None, max_length=10)

    @field_validator("official_url", "crest_source_url")
    @classmethod
    def safe_url(cls, value):
        return normalize_url(value) if value else value

    @field_validator("crest_url")
    @classmethod
    def safe_crest(cls, value):
        if not value:
            return value
        if value.startswith("/assets/"):
            from pathlib import PurePosixPath
            if "\\" in value or "%" in value or "?" in value or "#" in value or ".." in value.split("/"):
                raise ValueError("队徽地址应指向本地图片或公开图片网址")
            if PurePosixPath(value).suffix.lower() not in {".png", ".svg", ".jpg", ".jpeg", ".webp", ".avif"}:
                raise ValueError("请填写图片文件地址")
            return value
        return normalize_url(value)


def register_team_profile_routes(app, store):
    @app.get("/api/teams/{team_id}/profile")
    async def team_profile(team_id: str, mode: Literal["live", "demo"] | None = Query(None)):
        team = store.get("teams", team_id)
        if not team:
            raise HTTPException(404, "球队不存在")
        settings = store.settings()
        selected_mode = mode or settings["mode"]
        evidence = store.list("evidence", mode=selected_mode, team_id=team_id)
        if settings.get("spoiler_free", True):
            from .ingestion import safe_evidence_view
            evidence = [view for item in evidence if (view := safe_evidence_view(item))]
        knowledge = knowledge_for_team(store, team_id, selected_mode,
                                      spoiler_free=settings.get("spoiler_free", True), limit=None)
        timestamps = [item.get("fetched_at") for item in evidence if item.get("fetched_at")]
        return {"team": team, "mode": selected_mode, "knowledge": knowledge[:20],
                "library": {"evidence_count": len(evidence), "knowledge_count": len(knowledge),
                            "last_collected_at": max(timestamps) if timestamps else None}}

    @app.patch("/api/teams/{team_id}/profile")
    async def edit_team_profile(team_id: str, payload: TeamProfilePatch):
        with store.atomic():
            team = store.get("teams", team_id)
            if not team:
                raise HTTPException(404, "球队不存在")
            patch = payload.model_dump(exclude_unset=True)
            team.update(patch)
            team["profile_updated_at"] = _timestamp()
            team["profile_origin"] = "user"
            return store.put("teams", team)
