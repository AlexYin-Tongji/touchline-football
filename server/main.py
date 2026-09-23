"""Private browser/server HTTP application with explicit manual work."""
from __future__ import annotations

import asyncio
import copy
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import ingestion
from .seed import seed
from .storage import Store, provider_origin
from .build_info import BUILD_ID, VERSION

ROOT = Path(__file__).resolve().parent.parent
Mode = Literal["demo", "live"]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=200)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
EventKind = Literal["team", "injury", "transfer", "match"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LLMSettings(Model):
    base_url: str | None = Field(None, max_length=4096)
    model: str | None = Field(None, max_length=200)
    api_key: str | None = Field(None, max_length=8192)
    api_key_set: bool | None = None
    clear_api_key: bool = False
    protocol: Literal["chat_completions", "responses", "anthropic_messages"] | None = None
    send_temperature: bool | None = None
    max_output_tokens: int | None = Field(None, ge=256, le=16000)
    use_env_proxy: bool | None = None
    thinking_enabled: bool | None = None
    max_rounds: int | None = Field(None, ge=2, le=16)
    timeout_seconds: int | None = Field(None, ge=10, le=120)
    temperature: float | None = Field(None, ge=0, le=1)

    @field_validator("base_url")
    @classmethod
    def validate_base(cls, value):
        if value:
            from urllib.parse import urlsplit
            if urlsplit(value).query or urlsplit(value).fragment:
                raise ValueError("模型地址不支持查询参数或片段，密钥请填写在独立密钥栏")
            return ingestion.normalize_url(value, allow_local=True).rstrip("/")
        return value

    @field_validator("model", "api_key")
    @classmethod
    def trim(cls, value):
        if value is not None and any(ord(char) < 32 for char in value):
            raise ValueError("模型名称和密钥不能包含控制字符")
        return value.strip() if value else value


class AutomationSettings(Model):
    enabled: bool | None = None
    interval_minutes: int | None = Field(None, ge=15, le=1440)
    sync_matches: bool | None = None
    generate_brief: bool | None = None


class PersonalizationSettings(Model):
    interests: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]] | None = Field(None, max_length=12)
    reading_language: Literal["zh", "original"] | None = None
    fan_level: Literal["beginner", "regular", "expert"] | None = None
    briefing_window_hours: int | None = Field(None, ge=6, le=336)

    @field_validator("interests")
    @classmethod
    def unique_interests(cls, value):
        return list(dict.fromkeys(value)) if value is not None else None


class ReportSettings(Model):
    auto_generate: bool | None = None
    # Accepted for older clients; the worker now drains all due reports.
    max_per_cycle: int | None = Field(None, ge=1)


class LeagueSyncInput(Model):
    season_start_year: int | None = Field(None, ge=2000, le=2100)


class TeamMaintenanceInput(Model):
    parts: list[Literal["roster", "schedule", "standings", "portraits"]] | None = Field(None, min_length=1, max_length=4)


class SettingsPatch(Model):
    mode: Mode | None = None
    followed_teams: list[Identifier] | None = Field(None, max_length=100)
    spoiler_free: bool | None = None
    explanation_depth: Literal["brief", "standard", "deep"] | None = None
    auto_refresh: bool | None = None
    refresh_minutes: int | None = Field(None, ge=5, le=1440)
    llm: LLMSettings | None = None
    automation: AutomationSettings | None = None
    personalization: PersonalizationSettings | None = None
    reports: ReportSettings | None = None
    verification_enabled: bool | None = None
    verification_timeout_seconds: int | None = Field(None, ge=10, le=90)


class TestSettings(LLMSettings):
    pass


class TeamInput(Model):
    name: ShortText
    english_name: str = Field("", max_length=200)
    color: str = Field("#24644E", pattern=r"^#[0-9a-fA-F]{6}$")


class WatchInput(Model):
    watching: bool


class ReadInput(Model):
    read: bool


class ReadAllInput(Model):
    mode: Mode
    team_id: Identifier | None = None


class SourceInput(Model):
    team_id: Identifier
    name: ShortText
    url: str = Field(min_length=8, max_length=4096)
    kind: Literal["html", "rss"]
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        return ingestion.normalize_url(value)


class SourcePatch(Model):
    enabled: bool


class RefreshInput(Model):
    mode: Mode


def validate_timestamp(value):
    parsed = ingestion.published_time(value)
    if not parsed:
        raise ValueError("时间必须是含时区的 ISO 8601 格式")
    return parsed


class ImportInput(Model):
    team_id: Identifier
    title: Title
    text: str = Field(min_length=30, max_length=30000)
    url: str = Field("", max_length=4096)
    source_name: ShortText = "手动导入"
    published_at: str | None = None
    kind: EventKind = "team"
    stage: ShortText | None = None
    spoiler: bool = False
    event_id: Identifier | None = None

    @field_validator("text")
    @classmethod
    def text_length(cls, value):
        value = ingestion.clean_text(value)
        if len(value) < 30:
            raise ValueError("正文去除空白后至少需要 30 字符")
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        return ingestion.normalize_url(value) if value else ""

    @field_validator("published_at")
    @classmethod
    def validate_time(cls, value):
        return validate_timestamp(value) if value else None


class MatchInput(Model):
    team_id: Identifier
    opponent: ShortText
    competition: ShortText
    kickoff: str
    venue: Literal["home", "away", "neutral"]
    status: Literal["scheduled", "finished"] = "scheduled"
    score: str | None = Field(None, max_length=30)
    note: str = Field("", max_length=5000)
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=100)

    @field_validator("kickoff")
    @classmethod
    def time(cls, value):
        return validate_timestamp(value)

    @field_validator("score")
    @classmethod
    def score_text(cls, value):
        if value and not re.fullmatch(r"\d{1,2}\s*[-–:]\s*\d{1,2}(?:\s*\(\d{1,2}\s*[-–:]\s*\d{1,2}\))?", value):
            raise ValueError("比分请填写 2-1 或 2-2 (4-3) 等格式")
        return value or None

    @model_validator(mode="after")
    def scheduled_score(self):
        if self.status == "scheduled" and self.score:
            raise ValueError("尚未结束的比赛不能填写赛果")
        return self


class MatchPatch(Model):
    status: Literal["scheduled", "finished"] | None = None
    score: str | None = Field(None, max_length=30)
    note: str | None = Field(None, max_length=5000)
    evidence_ids: list[Identifier] | None = Field(None, max_length=100)


class RunInput(Model):
    mode: Mode
    team_id: Identifier
    question: str = Field(min_length=1, max_length=5000)
    skill: Literal["auto", "ask", "team_brief", "match_preview", "match_review", "event_watch", "transfer_check", "tactics_explainer", "news_report", "team_maintenance", "team-news", "team-information", "football-assistant"] = "auto"
    provider_id: str | None = Field(None, pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    model: str | None = Field(None, min_length=1, max_length=200)
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=5)
    match_id: Identifier | None = None
    conversation_id: Identifier | None = None
    allow_network: bool = True
    tracking_id: Identifier | None = None
    parent_run_id: Identifier | None = None

    @field_validator("question")
    @classmethod
    def nonempty_question(cls, value):
        if not value.strip():
            raise ValueError("请输入问题")
        return value.strip()


class ResearchInput(Model):
    team_id: Identifier
    query: str = Field("", max_length=500)
    limit: int = Field(6, ge=1, le=8)


class ReadCandidateInput(Model):
    team_id: Identifier
    candidate_id: Identifier
    event_id: Identifier | None = None
    match_id: Identifier | None = None


class CollectInput(Model):
    query: str = Field("", max_length=500)
    limit: int = Field(4, ge=1, le=6)


class TrackingInput(Model):
    team_id: Identifier
    title: Title
    query: str = Field(min_length=2, max_length=500)
    interval_minutes: int = Field(60, ge=30, le=1440)
    event_id: Identifier | None = None
    match_id: Identifier | None = None


class TrackingPatch(Model):
    title: Title | None = None
    query: str | None = Field(None, min_length=2, max_length=500)
    interval_minutes: int | None = Field(None, ge=30, le=1440)
    enabled: bool | None = None


class AutomationRunInput(Model):
    team_id: Identifier | None = None


class DismissInput(Model):
    dismissed: bool = True


class AutostartInput(Model):
    enabled: bool


class PersonalRefreshInput(Model):
    team_id: Identifier | None = None


class ManualOrganizeInput(Model):
    team_id: Identifier


def safe_settings(settings):
    result = copy.deepcopy(settings)
    result["llm"]["api_key_set"] = bool(result["llm"].pop("api_key", ""))
    return result


def create_app(db_path=None):
    from .web_runtime import WebRuntime, PrivateAccess
    runtime = WebRuntime.from_env()
    access = PrivateAccess(runtime)
    store = Store(db_path or Path(os.environ.get("TOUCHLINE_DATA_DIR", str(ROOT / "data"))) / "touchline.sqlite3")
    with store.atomic():
        seed(store)
    from .personal_app import install_personal_defaults, PersonalRefreshManager
    from .team_center import TeamMaintenanceManager
    install_personal_defaults(store)
    team_maintenance = TeamMaintenanceManager(store)
    personal = PersonalRefreshManager(store, team_maintenance)
    from .workflows import WorkflowManager
    workflows = WorkflowManager(store)
    tasks = {}
    refresh_lock = asyncio.Lock()
    from .automation import AutomationManager
    async def automation_run_request(payload):
        return await create_run(RunInput(**payload))
    automation = AutomationManager(store, automation_run_request)
    from .league_data import LeagueSyncManager
    from . import news_reports
    league_sync = LeagueSyncManager(store)

    def queue_reports(team_id=None):
        targets = [team_id] if team_id else store.settings().get("followed_teams", [])
        if store.settings().get("reports", {}).get("auto_generate", True):
            for target in targets:
                news_reports.enqueue_reports(store, team_id=target)

    @asynccontextmanager
    async def lifespan(app):
        from .agent_delegation import recover_tasks
        recover_tasks(store)
        workflows.recover()
        news_reports.recover_reports(store)
        await league_sync.close()
        for run in store.list("runs"):
            if run["status"] in {"queued", "running"}:
                run.update({"status": "failed", "error": "服务重启，本次运行已中断，请重新发起。", "finished_at": ingestion.now()})
                store.put("runs", run)
        for item in store.list("tracking", "live"):
            item.update(next_check=None)
            if item.get("status") == "running":
                item.update(status="idle" if item.get("enabled") else "paused",
                            last_error="服务重启，已有资料保留；请手动重新检查。")
            store.put("tracking", item)
        yield
        await workflows.close()
        await automation.close()
        await league_sync.close()
        await personal.close()
        await team_maintenance.close()
        active = list(tasks.values())
        for task, event in active:
            event.set()
            task.cancel()
        await asyncio.gather(*(task for task, event in active), return_exceptions=True)
        if getattr(store, "_agent_runtime", None):
            await store._agent_runtime.shutdown()
        store.close()

    app = FastAPI(title="看台 Touchline", version=VERSION, lifespan=lifespan,
                  docs_url=None if runtime.server_mode else "/docs", redoc_url=None,
                  openapi_url=None if runtime.server_mode else "/openapi.json")
    app.state.store = store
    app.state.run_tasks = tasks
    app.state.automation = automation
    app.state.league_sync = league_sync
    app.state.personal = personal
    app.state.workflows = workflows
    app.state.team_maintenance = team_maintenance
    app.state.runtime = runtime
    app.state.access = access
    access.register(app)
    from .model_config import register_routes as register_model_routes
    register_model_routes(app, store)
    from .team_profiles import register_team_profile_routes
    register_team_profile_routes(app, store)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=runtime.hosts)

    @app.middleware("http")
    async def application_request_boundaries(request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if (origin and not runtime.allows_origin(origin)) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "仅允许当前网站发起修改请求"}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path not in {"/api/health", "/api/auth/status", "/api/auth/login"}:
            if not access.authenticated(request):
                return JSONResponse({"detail": "请先登录你的私人看台。"}, status_code=401, headers={"Cache-Control": "no-store"})
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > 256 * 1024:
            return JSONResponse({"detail": "请求正文超过 256 KB 上限"}, status_code=413)
        if request.method in {"POST", "PUT", "PATCH"}:
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 256 * 1024:
                    return JSONResponse({"detail": "请求正文超过 256 KB 上限"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        errors = [".".join(str(item) for item in error["loc"][1:]) + ": " + error["msg"] for error in exc.errors()]
        return JSONResponse({"detail": "；".join(errors)}, status_code=422)

    @app.exception_handler(ingestion.IngestionError)
    async def ingestion_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def team_exists(team_id):
        if not store.get("teams", team_id):
            raise HTTPException(404, "球队不存在")

    def require(collection, identifier):
        value = store.get(collection, identifier)
        if not value:
            raise HTTPException(404, "记录不存在")
        return value

    def check_evidence(ids, team_id, mode="live"):
        for identifier in ids:
            evidence = store.get("evidence", identifier)
            if not evidence or evidence["mode"] != mode or evidence["team_id"] != team_id:
                raise HTTPException(400, "比赛引用的资料不存在，或不属于当前球队及模式")

    def evidence_response(item):
        if not store.settings().get("spoiler_free", True):
            return copy.deepcopy(item)
        view = ingestion.safe_evidence_view(item)
        if view is not None:
            return view
        return {key: item.get(key) for key in ("id", "mode", "team_id", "source_name", "published_at", "fetched_at", "origin")} | {
            "title": "一份包含赛果的资料", "text": "资料已保存，内容因避剧透设置隐藏。", "url": "",
            "spoiler": True, "spoiler_hidden": True, "redaction_note": "已保存，内容因避剧透设置隐藏。可调整设置后查看。"}

    def event_response(item):
        if not store.settings().get("spoiler_free", True):
            return copy.deepcopy(item)
        records = [store.get("evidence", eid) for eid in item.get("evidence_ids", [])]
        views = [ingestion.safe_evidence_view(record) for record in records if record]
        if any(view is None for view in views) or (item.get("spoiler") and not views):
            return {key: item.get(key) for key in ("id", "mode", "team_id", "updated_at", "watching", "read", "evidence_ids")} | {
                "title": "一项包含赛果的事件", "summary": "事件已保存，内容因避剧透设置隐藏。",
                "spoiler": True, "spoiler_hidden": True, "timeline": [], "kind": "match"}
        redacted = [view for view in views if view.get("spoiler_redacted")]
        if not redacted:
            return copy.deepcopy(item)
        latest = redacted[-1]
        return {**item, "title": latest["title"], "summary": latest["text"][:300],
            "change": "这份新闻保留了可阅读的段落，含赛果部分已隐藏。", "spoiler": False,
            "spoiler_redacted": True, "stage": "原文已部分隐藏", "versions": [], "uncertainties": [],
            "stage_suggestion": None, "timeline": [{"at": view.get("fetched_at"), "text": view["title"],
                "evidence_id": view["id"]} for view in views]}

    def import_response(result):
        return {**result, "evidence": evidence_response(result["evidence"]),
                "event": event_response(result["event"]) if result.get("event") else None}

    def discovery_response(result):
        if not store.settings().get("spoiler_free", True):
            return result
        candidates = []
        for candidate in result.get("candidates", []):
            if ingestion.spoiler_title(candidate.get("title", "")):
                continue
            view = copy.deepcopy(candidate)
            if ingestion.spoiler_sensitive(view.get("description", "")):
                view["description"] = "含赛果线索的摘要已隐藏，可读取原文的安全节选。"
            candidates.append(view)
        return {**result, "candidates": candidates}

    def has_hidden_evidence(run):
        redacted = set(run.get("redacted_evidence_ids", []))
        quote_ids = [quote.get("evidence_id") for claim in (run.get("verification") or {}).get("claims", [])
                     for quote in claim.get("quotes", [])]
        for identifier in set(run.get("evidence_ids", []) + quote_ids):
            raw = store.get("evidence", identifier)
            if raw and raw.get("spoiler"):
                if identifier not in redacted or ingestion.safe_evidence_view(raw) is None:
                    return True
        return False

    def run_response(run, with_evidence=False):
        if run.get("internal_report"):
            report = news_reports.get_report(store, run.get("report_id", ""))
            if not report or report.get("status") != "completed" or report.get("run_id") != run["id"]:
                return {key: run.get(key) for key in ("id", "team_id", "mode", "skill", "conversation_id", "created_at", "finished_at", "internal_report", "report_id")} | {
                    "status": run.get("status"), "answer": "这份报告正在整理或已经失效，请在报告中心查看当前版本。", "evidence_ids": [], "evidence": [],
                    "structured_answer": None, "steps": [], "verification": None, "warnings": [], "error": None}
        result = copy.deepcopy(run)
        # Version bindings can contain titles/URLs later hidden by privacy or
        # spoiler changes. They are server-only even for currently visible runs.
        result.pop("evidence_versions", None)
        # Editorial completeness feedback can quote material not cited in the
        # final article. Keep it internal; the reader sees one aggregate notice.
        result.pop("news_coverage", None)
        protect = store.settings().get("spoiler_free", True)
        source_ids = set(run.get("evidence_ids", [])) | {quote.get("evidence_id")
            for claim in (run.get("verification") or {}).get("claims", []) for quote in claim.get("quotes", []) if quote.get("evidence_id")}
        source_hidden = any(not (source := store.get("evidence", identifier)) or source.get("private") or source.get("deleted")
                            or source.get("visibility") in {"hidden", "deleted"} for identifier in source_ids)
        from .agent_delegation import source_version
        source_stale = False
        for identifier, expected in run.get("evidence_versions", {}).items():
            current = store.get("evidence", identifier)
            if current and identifier in run.get("redacted_evidence_ids", []):
                current = ingestion.safe_evidence_view(current)
            if source_version(current) != expected:
                source_stale = True
                break
        hidden_answer = protect and run.get("skill") != "match_review" and (run.get("spoiler") or has_hidden_evidence(run))
        if hidden_answer or source_hidden or source_stale:
            result.update(answer="这份回答的引用来源已不可见，内容暂时隐藏。" if source_hidden else "引用原文已更新，旧回答暂时隐藏，请重新查阅核对。" if source_stale else "这份历史回答可能包含赛果，已按当前避剧透设置隐藏。",
                          evidence_ids=[], spoiler_hidden=hidden_answer, source_hidden=source_hidden, source_stale=source_stale, steps=[], structured_answer=None,
                          warnings=[], error=None,
                          verification={"status": "not_requested", "claims": [], "warnings": ["核对记录按避剧透设置隐藏。"]})
        if with_evidence:
            selected_match = store.get("matches", run.get("match_id")) if run.get("match_id") else None
            result["evidence"] = []
            for identifier in result.get("evidence_ids", []):
                evidence = store.get("evidence", identifier)
                if (not evidence or evidence["mode"] != run["mode"] or evidence["team_id"] != run["team_id"]
                        or evidence.get("private") or evidence.get("deleted") or evidence.get("visibility") in {"hidden", "deleted"}):
                    continue
                explicit_review = run["skill"] == "match_review" and selected_match and identifier in selected_match.get("evidence_ids", [])
                view = evidence if not protect or explicit_review else ingestion.safe_evidence_view(evidence)
                if view:
                    result["evidence"].append(view)
        secrets = {store.settings()["llm"].get("api_key", "")}
        secrets.update(store.get_secret("provider_" + row["id"]) for row in store.list("model_profiles"))
        secrets.discard("")
        def clean(value):
            if isinstance(value, str):
                for secret in secrets:
                    value = value.replace(secret, "[密钥已隐藏]")
                return value
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items() if key != "api_key"}
            return value
        return clean(result)

    def tracking_response(item):
        result = copy.deepcopy(item)
        if store.settings().get("spoiler_free", True):
            run = store.get("runs", item.get("last_run_id")) if item.get("last_run_id") else None
            if run and (run.get("spoiler") or run.get("skill") == "match_review" or has_hidden_evidence(run)):
                result["summary"] = "本次解读可能包含赛果，已按避剧透设置隐藏。"
                result["spoiler_hidden"] = True
            for field in ("updates", "archived_updates"):
                result[field] = [update for update in result.get(field, [])
                    if not ((r := store.get("runs", update.get("run_id", ""))) and (r.get("spoiler") or has_hidden_evidence(r)))]
        return result

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.get("/api/harness")
    async def harness_status():
        from .model_harness import runtime_harness
        from .agent_delegation import runtime_for
        return {**runtime_harness(store).snapshot(), "agents": runtime_for(store).snapshot()}

    @app.get("/api/service")
    async def get_service():
        from .service_control import service_status
        return {**service_status(), **runtime.describe(), "url": runtime.public_url or "http://127.0.0.1:8765/",
                "version": VERSION, "build_id": BUILD_ID}

    @app.post("/api/service/shortcut")
    async def service_shortcut():
        if runtime.server_mode:
            raise HTTPException(409, "网站在服务器上运行，无需创建本机快捷方式。")
        from .service_control import create_desktop_shortcut
        try:
            return await asyncio.to_thread(create_desktop_shortcut)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.put("/api/service/autostart")
    async def service_autostart(payload: AutostartInput):
        if runtime.server_mode:
            raise HTTPException(409, "网站由托管平台管理运行，无需设置本机自启。")
        from .service_control import set_autostart
        try:
            return await asyncio.to_thread(set_autostart, payload.enabled)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/automation/run")
    async def run_automation(payload: AutomationRunInput):
        if payload.team_id:
            team_exists(payload.team_id)
        return automation.start(payload.team_id)

    @app.post("/api/recommendations/{identifier}/dismiss")
    async def dismiss_recommendation(identifier: str, payload: DismissInput):
        recommendation = require("recommendations", identifier)
        recommendation["dismissed"] = payload.dismissed
        return store.put("recommendations", recommendation)

    @app.get("/api/state")
    async def state(mode: Mode | None = Query(None), team_id: Identifier | None = Query(None),
                    view: Literal["full", "summary"] = "full"):
        settings = store.settings()
        mode = mode or settings["mode"]
        if team_id:
            team_exists(team_id)
            if mode == "live" and view == "full":
                from .league_data import materialize_team
                materialize_team(store, team_id)
        if view == "summary":
            from .model_config import MODEL_ROLES, role_selection
            legacy = bool(settings["llm"].get("base_url") and settings["llm"].get("model"))
            roles = {role: bool(role_selection(store, role)) or legacy for role in MODEL_ROLES}
            matches = store.list("matches", mode, team_id)
            for match in matches:
                match.pop("source_raw", None)
                if settings["spoiler_free"]:
                    match["evidence_ids"] = []
                    if match["status"] == "finished":
                        match.update(score=None, note="", spoiler_hidden=True, home_score=None, away_score=None,
                                     goals_for=None, goals_against=None, result=None, source_versions=[])
            return {"settings": safe_settings(settings), "teams": store.list("teams"), "runtime": runtime.describe(),
                    "matches": sorted(matches, key=lambda item: item["kickoff"]), "service": await get_service(),
                    "setup": {"model_configured": any(roles[role] for role in ("feed", "chat", "team")), "model_roles_configured": roles,
                              "live_evidence_count": store.count("evidence", "live")},
                    "events": [], "evidence": [], "runs": [], "reports": [], "sources": [], "tracking": [],
                    "source_catalog": [], "recommendations": [], "last_refresh": store.get_meta("last_refresh_" + mode),
                    "spoiler_hidden": {"events": 0, "evidence": 0, "runs": 0}}
        evidence = store.list("evidence", mode, team_id)
        events = store.list("events", mode, team_id)
        matches = store.list("matches", mode, team_id)
        for match in matches:
            match.pop("source_raw", None)
        runs = [run for run in store.list("runs", mode, team_id) if not run.get("internal_report")]
        hidden = {"events": 0, "evidence": 0, "runs": 0}
        if settings["spoiler_free"]:
            safe_views = {item["id"]: ingestion.safe_evidence_view(item) for item in evidence}
            blocked = {identifier for identifier, view in safe_views.items() if view is None}
            hidden["evidence"] = len(blocked)
            evidence = [view for view in safe_views.values() if view is not None]
            visible_events = []
            for item in events:
                ids = item.get("evidence_ids", [])
                if blocked.intersection(ids) or (item.get("spoiler") and not ids):
                    continue
                visible_events.append(event_response(item))
            hidden["events"] = len(events) - len(visible_events)
            events = visible_events
            for match in matches:
                match["evidence_ids"] = [identifier for identifier in match["evidence_ids"] if identifier not in blocked]
                if match["status"] == "finished":
                    match.update({"score": None, "note": "", "spoiler_hidden": True,
                        "home_score": None, "away_score": None, "goals_for": None, "goals_against": None,
                        "result": None, "source_versions": []})
            visible_runs = [run for run in runs if not run.get("spoiler") and run["skill"] != "match_review" and not has_hidden_evidence(run)]
            hidden["runs"] = len(runs) - len(visible_runs)
            runs = visible_runs
        from .research import source_catalog
        from .recommendations import get_recommendations
        from .service_control import service_status
        from .presentation import present_events
        events = present_events(store, events, evidence, runs, spoiler_free=settings["spoiler_free"])
        evidence = [{**item, **{key: value for key, value in ingestion.content_details(item).items() if key != "paragraphs"}}
                    for item in evidence]
        # User-facing state contains references, never externally copied bodies.
        evidence = [{**item, "text": "", "paragraphs": []} if item.get("origin") in {"web", "rss"} and not item.get("fixture_record") else item for item in evidence]
        catalog_team = team_id or (settings["followed_teams"][0] if settings["followed_teams"] else None)
        catalog = source_catalog(store, catalog_team) if catalog_team else []
        from .model_config import MODEL_ROLES, role_selection
        legacy_configured = bool(settings["llm"].get("base_url") and settings["llm"].get("model"))
        configured_roles = {role: bool(role_selection(store, role)) or legacy_configured for role in MODEL_ROLES}
        return {"settings": safe_settings(settings), "teams": store.list("teams"), "runtime": runtime.describe(),
                "reports": news_reports.list_reports(store, team_id=team_id, mode=mode), "report_budget": news_reports.report_budget(store),
                "events": sorted(events, key=lambda item: item["updated_at"], reverse=True),
                "evidence": evidence, "matches": sorted(matches, key=lambda item: item["kickoff"]),
                "sources": store.list("sources", mode, team_id), "runs": [run_response(run) for run in runs[-100:]],
                "last_refresh": store.get_meta("last_refresh_" + mode), "spoiler_hidden": hidden,
                "tracking": [tracking_response(item) for item in store.list("tracking", mode, team_id)],
                "source_catalog": catalog, "setup": {"model_configured": any(configured_roles[role] for role in ("feed", "chat", "team")), "model_roles_configured": configured_roles,
                    "live_evidence_count": len(store.list("evidence", "live"))},
                "recommendations": get_recommendations(store, mode), "automation_status": automation.status(), "service": await get_service()}

    @app.get("/api/evidence/{identifier}")
    async def get_evidence(identifier: Identifier):
        raw = require("evidence", identifier)
        if raw.get("private") or raw.get("deleted") or raw.get("visibility") in {"hidden", "deleted"}:
            raise HTTPException(404, "来源不可用。")
        item = evidence_response(raw)
        if item.get("origin") in {"web", "rss"} and not item.get("fixture_record"):
            return {**item, "text": "", "paragraphs": [], "link_only": True}
        return {**item, **ingestion.content_details(item)}

    @app.get("/api/feed")
    async def get_feed(team_id: Identifier | None = None, cursor: str | None = Query(None, max_length=512),
                       limit: int = Query(20, ge=1, le=40), topic: EventKind | None = None):
        from .personal_app import feed_view
        if team_id:
            team_exists(team_id)
        return feed_view(store, team_id, cursor, limit, topic=topic)

    @app.post("/api/feed/refresh", status_code=202)
    async def refresh_feed(payload: PersonalRefreshInput):
        if payload.team_id:
            team_exists(payload.team_id)
        targets = [payload.team_id] if payload.team_id else store.settings().get("followed_teams", [])
        states = [workflows.start("news", target) for target in targets]
        return {"started": any(item.get("started") for item in states), "workflows": states}

    @app.post("/api/feed/organize")
    async def organize_feed(payload: ManualOrganizeInput):
        team_exists(payload.team_id)
        result = workflows.start("news", payload.team_id)
        if result["status"] == "blocked":
            raise HTTPException(400, result["message"])
        return result

    @app.get("/api/workflows/{kind}/{team_id}")
    async def workflow_status(kind: Literal["news", "team"], team_id: Identifier):
        team_exists(team_id)
        return workflows.status(kind, team_id)

    @app.post("/api/workflows/{kind}/{team_id}/start", status_code=202)
    async def start_workflow(kind: Literal["news", "team"], team_id: Identifier):
        team_exists(team_id)
        return workflows.start(kind, team_id)

    @app.post("/api/workflows/{kind}/{team_id}/stop")
    async def stop_workflow(kind: Literal["news", "team"], team_id: Identifier):
        team_exists(team_id)
        return await workflows.stop(kind, team_id)

    @app.get("/api/teams/{team_id}/center")
    async def get_team_center(team_id: Identifier, reveal: bool = False):
        from .team_center import center_view
        team_exists(team_id)
        return center_view(store, team_id, reveal=reveal)

    @app.post("/api/teams/{team_id}/maintain", status_code=202)
    async def maintain_team_center(team_id: Identifier, payload: TeamMaintenanceInput = TeamMaintenanceInput()):
        team_exists(team_id)
        return workflows.start("team", team_id, parts=payload.parts)

    @app.get("/api/leagues")
    async def get_leagues(season_start_year: int | None = Query(None, ge=2000, le=2100)):
        from .league_data import league_view
        seasons = store.list("seasons")
        available = sorted({(s["season_start_year"], s["label"]) for s in seasons}, reverse=True)
        return {"available_seasons": [{"season_start_year": y, "label": label} for y, label in available],
                "leagues": [league_view(store, league["id"], season_start_year) for league in store.list("leagues")]}

    @app.post("/api/leagues/sync", status_code=202)
    async def sync_all_leagues(payload: LeagueSyncInput = LeagueSyncInput()):
        return league_sync.start(year=payload.season_start_year)

    @app.post("/api/leagues/{identifier}/sync", status_code=202)
    async def sync_one_league(identifier: Identifier, payload: LeagueSyncInput = LeagueSyncInput()):
        return league_sync.start(identifier, payload.season_start_year)

    @app.get("/api/reports")
    async def get_reports(team_id: Identifier | None = None, mode: Mode = "live"):
        if team_id:
            team_exists(team_id)
        return {"reports": news_reports.list_reports(store, team_id=team_id, mode=mode), "budget": news_reports.report_budget(store)}

    @app.get("/api/reports/{identifier}")
    async def get_report(identifier: Identifier):
        report = news_reports.get_report(store, identifier)
        if not report:
            raise HTTPException(404, "报告不存在或当前不可见。")
        return report

    @app.post("/api/reports/{identifier}/retry")
    async def retry_report(identifier: Identifier):
        report = news_reports.retry_report(store, identifier)
        if not report:
            raise HTTPException(404, "报告不存在或当前不可见。")
        state = workflows.start("news", report["team_id"])
        if state["status"] == "blocked":
            raise HTTPException(400, state["message"])
        return report

    @app.put("/api/settings")
    async def settings_update(payload: SettingsPatch):
        patch = payload.model_dump(exclude_unset=True, exclude_none=True)
        for identifier in patch.get("followed_teams", []):
            team_exists(identifier)
        if "followed_teams" in patch:
            patch["followed_teams"] = list(dict.fromkeys(patch["followed_teams"]))
        saved = store.save_settings(patch)
        if patch.get("automation", {}).get("enabled") is False:
            await automation.close()
        return safe_settings(saved)

    @app.post("/api/settings/test")
    async def settings_test(payload: TestSettings):
        from .agent import test_connection
        config = store.settings()["llm"]
        supplied = payload.model_dump(exclude_none=True, exclude_unset=True)
        if supplied.get("base_url"):
            new_url = ingestion.normalize_url(supplied["base_url"], allow_local=True).rstrip("/")
            if provider_origin(new_url) != provider_origin(config["base_url"]):
                config["api_key"] = ""
            config["base_url"] = new_url
        for key in ("model", "api_key"):
            if supplied.get(key):
                config[key] = supplied[key].strip()
        for key in ("protocol", "send_temperature", "max_output_tokens", "use_env_proxy", "thinking_enabled", "temperature", "max_rounds", "timeout_seconds"):
            if key in supplied:
                config[key] = supplied[key]
        try:
            validated = LLMSettings(**config).model_dump(exclude_none=True)
        except ValueError:
            raise HTTPException(422, "模型配置格式不正确，请检查地址、名称与密钥") from None
        from .model_harness import runtime_harness
        result = await test_connection(validated, dispatcher=runtime_harness(store))
        for secret in {config.get("api_key", ""), store.settings()["llm"].get("api_key", "")} - {""}:
            result["message"] = result.get("message", "").replace(secret, "[密钥已隐藏]")
        return result

    @app.get("/api/source-catalog")
    async def get_source_catalog(team_id: Identifier):
        from .research import source_catalog
        team_exists(team_id)
        return {"sources": source_catalog(store, team_id)}

    @app.post("/api/research")
    async def research_news(payload: ResearchInput):
        from .research import discover_news
        team_exists(payload.team_id)
        return discovery_response(await discover_news(store, payload.team_id, payload.query, payload.limit))

    @app.post("/api/research/read")
    async def research_read(payload: ReadCandidateInput):
        from .research import read_candidate
        team_exists(payload.team_id)
        result = import_response(await read_candidate(store, **payload.model_dump()))
        queue_reports(payload.team_id)
        return result

    @app.post("/api/teams/{team_id}/collect")
    async def collect_team_news(team_id: str, payload: CollectInput):
        from .research import collect_team
        team_exists(team_id)
        async with refresh_lock:
            synced = None
            if store.settings().get("automation", {}).get("sync_matches", True):
                from .fixtures import sync_team
                synced = await sync_team(store, team_id)
            result = discovery_response(await collect_team(store, team_id, query=payload.query, limit=payload.limit))
            if synced is not None:
                result["match_sync"] = synced
            queue_reports(team_id)
            return result

    @app.post("/api/teams/{team_id}/sync")
    async def sync_team_matches(team_id: str):
        from .fixtures import sync_team
        team_exists(team_id)
        async with refresh_lock:
            return await sync_team(store, team_id, force=True)

    @app.post("/api/teams")
    async def create_team(payload: TeamInput):
        identifier = "team_" + uuid.uuid4().hex[:12]
        return store.put("teams", {"id": identifier, "name": payload.name, "short_name": (payload.english_name[:3] or payload.name[:2]).upper(),
                        "english_name": payload.english_name, "color": payload.color, "league": "自定义球队"})

    @app.post("/api/events/read-all")
    async def read_all(payload: ReadAllInput):
        if payload.team_id:
            team_exists(payload.team_id)
        for event in store.list("events", payload.mode, payload.team_id):
            if store.settings()["spoiler_free"] and event.get("spoiler"):
                continue
            event["read"] = True
            store.put("events", event)
        return {"ok": True}

    @app.post("/api/events/{identifier}/watch")
    async def watch(identifier: str, payload: WatchInput):
        with store.atomic():
            event = require("events", identifier)
            event["watching"] = payload.watching
            return event_response(store.put("events", event))

    @app.post("/api/events/{identifier}/read")
    async def mark_read(identifier: str, payload: ReadInput):
        with store.atomic():
            event = require("events", identifier)
            event["read"] = payload.read
            return event_response(store.put("events", event))

    @app.post("/api/sources")
    async def create_source(payload: SourceInput):
        team_exists(payload.team_id)
        if len(store.list("sources", "live")) >= 100:
            raise HTTPException(400, "首版最多配置 100 个来源，请先停用并删除不需要的来源")
        if any(item["team_id"] == payload.team_id and item["url"] == payload.url for item in store.list("sources", "live")):
            raise HTTPException(400, "该球队已经配置了这个来源")
        return store.put("sources", {"id": "source_" + uuid.uuid4().hex[:16], "mode": "live", **payload.model_dump(),
                         "last_checked": None, "last_error": None, "last_success": None, "last_added": 0})

    @app.patch("/api/sources/{identifier}")
    async def update_source(identifier: str, payload: SourcePatch):
        source = require("sources", identifier)
        source["enabled"] = payload.enabled
        return store.put("sources", source)

    @app.delete("/api/sources/{identifier}")
    async def delete_source(identifier: str):
        require("sources", identifier)
        store.delete("sources", identifier)
        return {"ok": True}

    @app.post("/api/sources/{identifier}/refresh")
    async def refresh_one(identifier: str):
        source = require("sources", identifier)
        async with refresh_lock:
            result = await ingestion.refresh_source(store, identifier)
            store.set_meta("last_refresh_live", ingestion.now())
            queue_reports(source["team_id"])
            return result

    @app.post("/api/refresh")
    async def refresh(payload: RefreshInput):
        if payload.mode == "demo":
            return {"added": 0, "unchanged": 0, "errors": [], "message": "虚构示例不采集网络资料。"}
        async with refresh_lock:
            result = await ingestion.refresh_all(store)
            queue_reports()
            return result

    @app.post("/api/import")
    async def import_text(payload: ImportInput):
        team_exists(payload.team_id)
        result = import_response(ingestion.ingest(store, **payload.model_dump()))
        queue_reports(payload.team_id)
        return result

    @app.post("/api/matches")
    async def create_match(payload: MatchInput):
        team_exists(payload.team_id)
        check_evidence(payload.evidence_ids, payload.team_id)
        return store.put("matches", {"id": "match_" + uuid.uuid4().hex[:16], "mode": "live", **payload.model_dump()})

    @app.patch("/api/matches/{identifier}")
    async def update_match(identifier: str, payload: MatchPatch):
        match = require("matches", identifier)
        if match["mode"] != "live":
            raise HTTPException(400, "虚构示例比赛不可修改，请在真实资料模式录入")
        patch = payload.model_dump(exclude_unset=True)
        if match.get("source_match_id") and any(key in patch and patch[key] != match.get(key) for key in ("score", "status")):
            raise HTTPException(400, "官方比赛的赛果与状态由来源同步更新；可以编辑备注和关联资料。")
        if "evidence_ids" in patch and patch["evidence_ids"] is not None and store.settings()["spoiler_free"]:
            hidden_ids = [identifier for identifier in match.get("evidence_ids", [])
                          if (evidence := store.get("evidence", identifier)) and evidence.get("spoiler")]
            patch["evidence_ids"] = list(dict.fromkeys(patch["evidence_ids"] + hidden_ids))
        merged = {key: value for key, value in match.items() if key in MatchInput.model_fields}
        merged.update(patch)
        if match.get("source_match_id") and not match.get("kickoff"):
            # A date-only fixture must remain editable for notes/evidence without
            # inventing a midnight kickoff to satisfy the manual-match schema.
            if patch.get("score") or patch.get("status") not in {None, match.get("status")}:
                raise HTTPException(422, "开赛时间尚未确认，请同步官方资料后再补比赛结果。")
            check_evidence(patch.get("evidence_ids", match.get("evidence_ids", [])), match["team_id"])
            match.update(patch)
            return store.put("matches", match)
        try:
            validated = MatchInput.model_validate(merged)
        except ValueError:
            raise HTTPException(422, "比赛信息不完整、比分格式不正确，或未结束比赛填写了赛果") from None
        check_evidence(validated.evidence_ids, match["team_id"])
        match.update(validated.model_dump())
        return store.put("matches", match)

    async def execute(run_id, cancel_event):
        try:
            from .agent import run_agent
            run = store.get("runs", run_id)
            if run and run.get("tracking_id") and run.get("collection_pending"):
                from .tracking import execute_tracking
                await execute_tracking(store, run_id, cancel_event)
            else:
                await run_agent(store, run_id, cancel_event)
        except asyncio.CancelledError:
            run = store.get("runs", run_id)
            if run and run["status"] not in {"completed", "failed"}:
                run.update({"status": "cancelled", "finished_at": ingestion.now(), "error": "用户已停止本次运行。"})
                store.put("runs", run)
        except Exception:
            run = store.get("runs", run_id)
            if run:
                run.update({"status": "failed", "finished_at": ingestion.now(), "error": "执行失败，请检查模型配置并重试。"})
                store.put("runs", run)
        finally:
            tasks.pop(run_id, None)

    def launch_tracking(item):
        current = require("tracking", item["id"])
        previous_id = current.get("last_run_id")
        if previous_id and previous_id in tasks:
            return require("runs", previous_id)
        if len(tasks) >= 3:
            raise HTTPException(503, "已有三个任务正在执行，请稍后再检查。")
        run = {"id": "run_" + uuid.uuid4().hex[:16], "mode": "live", "team_id": current["team_id"],
            "tracking_id": current["id"], "match_id": current.get("match_id"), "skill": "event_watch",
            "question": current["query"], "conversation_id": "tracking_" + current["id"] + "_v" + str(current.get("revision", 1)),
            "status": "queued", "answer": "", "evidence_ids": [], "warnings": [], "steps": [], "error": None,
            "created_at": ingestion.now(), "finished_at": None, "collection_pending": True,
            "model": store.settings()["llm"]["model"], "protocol": store.settings()["llm"].get("protocol", "chat_completions"),
            "skills_used": [], "allow_network": True, "spoiler": not store.settings()["spoiler_free"]}
        store.put("runs", run)
        current.update(status="running", last_run_id=run["id"], last_error=None)
        store.put("tracking", current)
        event = asyncio.Event()
        task = asyncio.create_task(execute(run["id"], event))
        tasks[run["id"]] = (task, event)
        return run

    @app.post("/api/tracking")
    async def create_tracking(payload: TrackingInput):
        team_exists(payload.team_id)
        if not payload.query.strip():
            raise HTTPException(422, "请填写需要跟进的具体主题。")
        if len(store.list("tracking", "live")) >= 30:
            raise HTTPException(400, "最多保留30个跟进任务，请先整理不再需要的任务。")
        for collection, identifier in (("events", payload.event_id), ("matches", payload.match_id)):
            if identifier:
                linked = require(collection, identifier)
                if linked["mode"] != "live" or linked["team_id"] != payload.team_id:
                    raise HTTPException(400, "关联对象不属于当前球队的真实资料。")
        item = {"id": "track_" + uuid.uuid4().hex[:16], "mode": "live", **payload.model_dump(),
            "query": payload.query.strip(), "enabled": True, "status": "idle", "last_checked": None,
            "next_check": None, "last_run_id": None, "last_error": None, "summary": "请手动开始首次检查。",
            "baseline_evidence_ids": [], "updates": [], "revision": 1, "created_at": ingestion.now()}
        return store.put("tracking", item)

    @app.patch("/api/tracking/{identifier}")
    async def update_tracking(identifier: str, payload: TrackingPatch):
        item = require("tracking", identifier)
        patch = payload.model_dump(exclude_none=True, exclude_unset=True)
        if "query" in patch:
            patch["query"] = patch["query"].strip()
            if not patch["query"]:
                raise HTTPException(422, "跟进主题不能为空。")
        if "enabled" in patch and not patch["enabled"] and item.get("last_run_id") in tasks:
            await cancel_run(item["last_run_id"])
            item = require("tracking", identifier)
        if "query" in patch and patch["query"] != item["query"]:
            if item.get("last_run_id") in tasks:
                await cancel_run(item["last_run_id"])
                item = require("tracking", identifier)
            archived = item.get("archived_updates", []) + [{**update, "query": item["query"]} for update in item.get("updates", [])]
            item.update(baseline_evidence_ids=[], last_checked=None, next_check=None,
                revision=item.get("revision", 1) + 1, summary="跟进主题已修改，等待建立新的资料基线。",
                last_run_id=None, updates=[], archived_updates=archived[-100:], event_id=None, match_id=None)
        item.update(patch)
        if not item["enabled"]:
            item["status"] = "paused"
        elif item["status"] == "paused":
            item.update(status="idle")
        item["next_check"] = None
        return tracking_response(store.put("tracking", item))

    @app.post("/api/tracking/{identifier}/check")
    async def check_tracking(identifier: str):
        return run_response(launch_tracking(require("tracking", identifier)))

    @app.delete("/api/tracking/{identifier}")
    async def delete_tracking(identifier: str):
        item = require("tracking", identifier)
        if item.get("last_run_id") in tasks:
            await cancel_run(item["last_run_id"])
        store.delete("tracking", identifier)
        return {"ok": True}

    @app.get("/api/conversations")
    async def conversations(team_id: Identifier | None = None, mode: Mode = "live",
                            cursor: str | None = Query(None, max_length=512), limit: int = Query(30, ge=1, le=100)):
        from .conversations import list_conversations
        if team_id:
            team_exists(team_id)
        return list_conversations(store, team_id=team_id, mode=mode, cursor=cursor, limit=limit)

    @app.get("/api/conversations/{identifier}")
    async def conversation(identifier: Identifier):
        from .conversations import conversation_runs, summarize
        rows = conversation_runs(store, identifier)
        if not rows:
            raise HTTPException(404, "对话不存在。")
        return {"conversation": summarize(rows, store), "runs": [run_response(row, with_evidence=True) for row in rows]}

    @app.post("/api/runs")
    async def create_run(payload: RunInput):
        team_exists(payload.team_id)
        check_evidence(payload.evidence_ids, payload.team_id, payload.mode)
        if len(tasks) >= 3:
            raise HTTPException(503, "已有 3 个任务运行中，请停止或等待后再试")
        match = store.get("matches", payload.match_id) if payload.match_id else None
        if payload.match_id and not match:
            league_match = store.get("league_matches", payload.match_id)
            if not league_match:
                raise HTTPException(404, "比赛不存在。")
            if payload.mode != "live" or payload.team_id not in {league_match["home_team_id"], league_match["away_team_id"]}:
                raise HTTPException(400, "比赛不属于当前球队及资料模式。")
            from .league_data import materialize_team
            materialize_team(store, payload.team_id, league_match["season_id"])
            match = store.get("matches", league_match["id"] + "-" + payload.team_id)
            if not match:
                home = league_match["home_team_id"] == payload.team_id
                opponent = store.get("teams", league_match["away_team_id"] if home else league_match["home_team_id"]) or {}
                names = {str(name).casefold() for name in [opponent.get("name", ""), opponent.get("english_name", ""), *opponent.get("aliases", [])] if name}
                candidates = [row for row in store.list("matches", "live", payload.team_id)
                              if row.get("venue") == ("home" if home else "away") and row.get("kickoff") == league_match.get("kickoff")
                              and str(row.get("opponent", "")).casefold() in names]
                if len(candidates) == 1:
                    match = candidates[0]
            if not match:
                raise HTTPException(409, "这场比赛的资料正在更新，请稍后再试。")
            payload = payload.model_copy(update={"match_id": match["id"]})
        if match and (match["mode"] != payload.mode or match["team_id"] != payload.team_id):
            raise HTTPException(400, "比赛不属于当前球队及资料模式")
        if payload.skill in {"match_preview", "match_review"} and not match:
            raise HTTPException(400, "请先选择一场已录入的比赛")
        if payload.skill == "match_review" and match["status"] != "finished":
            raise HTTPException(400, "比赛尚未标记结束，不能生成赛后复盘")
        if payload.conversation_id:
            from .conversations import conversation_runs
            prior = conversation_runs(store, payload.conversation_id)
            if not prior:
                # Do not join a background job's context by guessing its identifier.
                reserved = [run for run in store.list("runs") if run.get("conversation_id") == payload.conversation_id]
                if reserved:
                    raise HTTPException(400, "后台任务不能作为个人对话继续，请开始新对话。")
            if any(run["mode"] != payload.mode or run["team_id"] != payload.team_id for run in prior):
                raise HTTPException(400, "会话不属于当前球队及模式，请开始新会话")
            if any(run.get("status") in {"queued", "running"} for run in prior):
                raise HTTPException(409, "这段对话仍在回答中，请等待完成或先停止回答。")
        else:
            prior = []
        from .model_config import role_selection
        model_role = prior[0].get("model_role", "chat") if prior else ("team" if payload.skill in {"team_maintenance", "team-information"} else "feed" if payload.skill == "team-news" else "chat")
        active_model = role_selection(store, model_role) or {}
        from .conversations import conversation_binding
        bound = conversation_binding(store, prior, strict=True) if payload.mode == "live" else None
        provider_id = bound["provider_id"] if bound else payload.provider_id or active_model.get("provider_id")
        chosen_model = bound["model"] if bound else payload.model or active_model.get("model")
        if bound and ((payload.provider_id and payload.provider_id != provider_id) or (payload.model and payload.model != chosen_model)):
            raise HTTPException(409, "当前对话使用原先选择的模型；切换模型请开启新对话。")
        model_config = store.settings()["llm"]
        if provider_id and payload.mode == "live":
            from .model_config import effective_config
            from .provider import AgentFailure
            try:
                model_config = effective_config(store, provider_id, chosen_model)
            except AgentFailure as exc:
                raise HTTPException(400, str(exc)) from None
        if payload.parent_run_id:
            parent = require("runs", payload.parent_run_id)
            if parent.get("internal_report") or parent.get("report_id") or parent.get("tracking_id"):
                raise HTTPException(400, "后台任务不能作为个人对话上下文继续。")
            if parent["mode"] != payload.mode or parent["team_id"] != payload.team_id or parent.get("match_id") != payload.match_id:
                raise HTTPException(400, "延续任务必须属于同一球队、资料空间和比赛。")
            if parent.get("model_role", "chat") != model_role or (payload.conversation_id and (parent.get("conversation_id") or parent["id"]) != payload.conversation_id):
                raise HTTPException(400, "延续任务必须来自当前对话及同一模型分工。")
        if payload.tracking_id:
            tracking = require("tracking", payload.tracking_id)
            if tracking["team_id"] != payload.team_id or payload.mode != "live":
                raise HTTPException(400, "跟进任务与当前球队或资料空间不一致。")
        run = {"id": "run_" + uuid.uuid4().hex[:16], **payload.model_dump(),
               "conversation_id": payload.conversation_id or "conversation_" + uuid.uuid4().hex[:16],
               "status": "queued", "answer": "", "input_evidence_ids": list(dict.fromkeys(payload.evidence_ids)), "evidence_ids": [], "warnings": [], "steps": [], "error": None,
               "created_at": ingestion.now(), "finished_at": None,
               "provider_id": provider_id if payload.mode == "live" else None,
               "model_role": model_role,
               "model": "规则演示，未调用模型" if payload.mode == "demo" else model_config["model"],
               "spoiler": payload.skill == "match_review" or not store.settings()["spoiler_free"],
               "skills_used": [], "protocol": model_config.get("protocol", "chat_completions")}
        store.put("runs", run)
        event = asyncio.Event()
        task = asyncio.create_task(execute(run["id"], event))
        tasks[run["id"]] = (task, event)
        return run_response(run)

    @app.post("/api/runs/{identifier}/resume")
    async def resume_run(identifier: str):
        previous = require("runs", identifier)
        if previous.get("internal_report"):
            raise HTTPException(409, "请从报告中心重新整理这份报告。")
        if previous["status"] in {"queued", "running"}:
            raise HTTPException(409, "本次任务仍在执行，可以继续等待或先停止。")
        if previous.get("tracking_id") and previous.get("collection_pending"):
            return run_response(launch_tracking(require("tracking", previous["tracking_id"])))
        return await create_run(RunInput(mode=previous["mode"], team_id=previous["team_id"],
            question=previous["question"], skill=previous.get("skill", "auto"), match_id=previous.get("match_id"),
            conversation_id=previous.get("conversation_id"), allow_network=previous.get("allow_network", True),
            parent_run_id=previous["id"], evidence_ids=previous.get("input_evidence_ids", [])))

    @app.get("/api/runs/{identifier}")
    async def get_run(identifier: str):
        return run_response(require("runs", identifier), with_evidence=True)

    @app.post("/api/runs/{identifier}/cancel")
    async def cancel_run(identifier: str):
        run = require("runs", identifier)
        if run.get("internal_report"):
            raise HTTPException(409, "这是动态整理任务；请在动态页点击结束，停止当前工作。")
        if run["status"] in {"completed", "failed", "cancelled"}:
            return run_response(run, with_evidence=True)
        active = tasks.get(identifier)
        if active:
            task, event = active
            event.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        run = require("runs", identifier)
        run.update({"status": "cancelled", "finished_at": ingestion.now(), "error": "用户已停止本次运行。"})
        store.put("runs", run)
        return run_response(run, with_evidence=True)

    @app.get("/api/capabilities")
    async def capabilities():
        from .agent import CAPABILITIES
        return CAPABILITIES

    prototype = ROOT / "prototype"
    if prototype.is_dir() and not runtime.server_mode:
        app.mount("/prototype", StaticFiles(directory=prototype, html=True), name="prototype")
    assets = ROOT / "dist" / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}")
    async def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        public_file = (ROOT / "dist" / path).resolve()
        if public_file.is_relative_to((ROOT / "dist").resolve()) and public_file.is_file():
            return FileResponse(public_file)
        index = ROOT / "dist" / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse({"detail": "前端尚未构建，请先启动开发前端或完成构建。原型位于 /prototype/。"}, status_code=503)

    return app


_application = None


async def app(scope, receive, send):
    """Initialize storage when serving, never merely when importing test helpers."""
    global _application
    if _application is None:
        _application = create_app()
    application = _application
    try:
        await application(scope, receive, send)
    finally:
        if scope["type"] == "lifespan":
            _application = None

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server.main:app", host="127.0.0.1", port=8765, reload=False)
