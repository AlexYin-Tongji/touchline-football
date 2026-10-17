"""Small provider catalog, write-only credentials, and per-conversation models."""
from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from .ingestion import normalize_url
from .provider import PROTOCOLS, AgentFailure, endpoint_url, http_failure
from .storage import provider_origin


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModelEntry(StrictModel):
    """One model a provider offers, and the three limits worth stating.

    ``max_output_tokens`` used to be capped at 16000, and ``provider.make_payload``
    clamped to the same number no matter what was stored. A plan that allows 32K
    or 128K therefore could not be asked for more, and the failure surfaced as
    ``finish_reason=length`` with an error telling the reader to raise a value the
    form would not accept. The caps here are the storage ceiling, not a claim
    about any model; the provider's own limit is the authority.

    ``context_window`` is recorded because the settings page has to show *some*
    number when a model overflows, and guessing one from the protocol is worse
    than knowing it. It is never used to truncate input: dropping source text
    silently would turn a gap into an incomplete report.
    """
    id: str = Field(min_length=1, max_length=200)
    name: str = Field("", max_length=200)
    max_output_tokens: int | None = Field(None, ge=256, le=131072)
    context_window: int | None = Field(None, ge=1024, le=10485760)
    # Wire parameters, stored per model because they describe the model rather
    # than the provider: two models behind one gateway disagree about whether
    # they accept temperature at all. `None` means "not sent", which is not the
    # same as sending a default — the DeepSeek Harness form makes the same
    # distinction and its adapter refuses an explicit empty value.
    temperature: float | None = Field(None, ge=0, le=2)
    send_temperature: bool = False
    reasoning_effort: str | None = Field(None, max_length=40)
    thinking_enabled: bool | None = None
    timeout_seconds: int | None = Field(None, ge=10, le=300)

    @field_validator("reasoning_effort")
    @classmethod
    def effort_value(cls, value):
        if value is None:
            return None
        allowed = {"minimal", "low", "medium", "high", "xhigh", "max"}
        if value not in allowed:
            raise ValueError("推理等级需为 minimal、low、medium、high、xhigh 或 max 之一")
        return value

    @field_validator("id", "name")
    @classmethod
    def clean(cls, value):
        if any(ord(c) < 32 for c in value):
            raise ValueError("模型字段不能包含控制字符")
        return value.strip()


class ProviderInput(StrictModel):
    id: str = Field(default_factory=lambda: "provider_" + uuid.uuid4().hex)
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=8, max_length=4096)
    protocol: str = "chat_completions"
    api_key: str | None = Field(None, max_length=8192)
    clear_api_key: bool = False
    models: list[ModelEntry] = Field(min_length=1, max_length=100)

    @field_validator("id", mode="before")
    @classmethod
    def identifier(cls, value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", value):
            raise PydanticCustomError(
                "provider_id",
                "服务商内部标识须以小写英文字母开头，只能包含小写字母、数字、短横线或下划线，最多 48 个字符。新增服务商会自动生成标识，请刷新页面后重试。",
            )
        return value

    @field_validator("name")
    @classmethod
    def display_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("请填写有效的服务商名称")
        return value

    @field_validator("base_url")
    @classmethod
    def base(cls, value):
        if urlsplit(value).query or urlsplit(value).fragment:
            raise ValueError("模型服务地址不能包含查询参数或片段")
        return normalize_url(value, allow_local=True).rstrip("/")

    @field_validator("protocol")
    @classmethod
    def protocol_value(cls, value):
        if value not in PROTOCOLS:
            raise ValueError("不支持的接口协议")
        return value

    @field_validator("api_key")
    @classmethod
    def key_value(cls, value):
        if value is not None and any(ord(c) < 32 for c in value):
            raise ValueError("API密钥不能包含控制字符")
        return value.strip() if value else value

    @field_validator("models")
    @classmethod
    def unique_models(cls, values):
        if any(not v.id for v in values) or len({v.id for v in values}) != len(values):
            raise ValueError("模型ID必须非空且不能重复")
        return values


class DiscoverInput(StrictModel):
    provider_id: str | None = Field(None, max_length=48)
    base_url: str = Field(min_length=8, max_length=4096)
    protocol: str = "chat_completions"
    api_key: str | None = Field(None, max_length=8192)
    _base = field_validator("base_url")(ProviderInput.base.__func__)
    _protocol = field_validator("protocol")(ProviderInput.protocol_value.__func__)
    _key = field_validator("api_key")(ProviderInput.key_value.__func__)


class SelectInput(StrictModel):
    provider_id: str = Field(min_length=1, max_length=48)
    model: str = Field(min_length=1, max_length=200)


# There is one model. The six-role split used to resolve `feed`, `chat`, `team`,
# `verify`, `photos` and `review` against separate assignments, which is how the
# settings page could show one active model while background work ran on another
# (measured: `active_model` = gemini-4-argon, every role = gemini-3.8-flash).
# `model_roles` is no longer read. The key is left in the database untouched so a
# rollback to a tagged build still finds its assignments, but nothing here reads
# it and nothing writes it.
#
# `role_selection` survives as a one-argument shim because twelve call sites pass
# a role it now ignores. Deleting the parameter would have meant editing all of
# them in the same commit as the migration, which is the change most likely to be
# rolled back on its own.
def role_selection(store, role=None):
    """The active model, for every caller that used to name a role."""
    read_meta = getattr(store, "get_meta", lambda key: None)
    return copy.deepcopy(read_meta("active_model"))


def background_flags(store):
    """Whether the two unattended model passes may run.

    Verification runs inside a task the reader started, so it stays a property of
    that task. Photo planning and the review loop instead run on a team refresh
    the reader did not initiate. They used to be free because they were not
    inherited; with one model they would inherit, so the cost moves to these two
    switches, both off until turned on. Stored as a metadata key rather than a
    settings section because `storage.settings()` merges a fixed list of nested
    sections and a new one added to one code path but not the other disappears
    silently.
    """
    saved = store.get_meta("background_model_tasks", {}) if hasattr(store, "get_meta") else {}
    saved = saved if isinstance(saved, dict) else {}
    return {"photos": saved.get("photos") is True, "review": saved.get("review") is True}


def background_config(store, kind):
    """The active model's config, or None when that background pass is off.

    The config is the real one — base URL, key, per-model parameters — because
    the caller is about to send a request with it. An empty dict would read as
    "configured" to ``news_reports._configured`` and fail at request time with a
    message about a missing address.
    """
    if not background_flags(store).get(kind):
        return None
    selected = role_selection(store)
    if not selected:
        return None
    try:
        return effective_config(store, selected["provider_id"], selected["model"])
    except AgentFailure:
        return None


def verification_config(store, task_config):
    """The checker runs on the same model as the writer now.

    It used to be resolvable separately so that a cheap model could write and a
    stronger one could check. With one configured model there is nothing to
    resolve, and pretending otherwise would be the same silent divergence this
    change removes.
    """
    return copy.deepcopy(task_config)


def role_settings(store, role=None):
    settings = copy.deepcopy(store.settings())
    selected = role_selection(store)
    if selected:
        settings["llm"] = effective_config(store, selected["provider_id"], selected["model"])
    return settings


class TestInput(StrictModel):
    model: str = Field(min_length=1, max_length=200)


class BackgroundInput(StrictModel):
    photos: bool | None = None
    review: bool | None = None


class TestProviderInput(StrictModel):
    """A provider form as the reader currently sees it, saved or not.

    ``id`` is optional because the create card has none yet; ``api_key`` is
    optional because a blank field on an edit means "keep the stored key". A
    blank key on a create means keyless, which some local gateways really are.
    """
    id: str | None = Field(None, max_length=48)
    provider_id: str | None = Field(None, max_length=48)
    base_url: str = Field(min_length=8, max_length=4096)
    protocol: str = "chat_completions"
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(None, max_length=8192)
    models: list[ModelEntry] = Field(default_factory=list, max_length=100)

    _base = field_validator("base_url")(ProviderInput.base.__func__)
    _protocol = field_validator("protocol")(ProviderInput.protocol_value.__func__)
    _key = field_validator("api_key")(ProviderInput.key_value.__func__)


def migrate_legacy(store):
    if store.get_meta("model_profiles_migrated"):
        return
    llm = store.settings()["llm"]
    if llm.get("base_url") and llm.get("model") and not store.list("model_profiles"):
        profile = {"id": "existing", "name": "现有模型服务", "base_url": llm["base_url"], "protocol": llm.get("protocol", "chat_completions"),
                   "models": [{"id": llm["model"], "name": llm["model"], "max_output_tokens": llm.get("max_output_tokens", 16000)}],
                   "created_at": datetime.now(timezone.utc).isoformat()}
        store.put("model_profiles", profile)
        store.set_secret("provider_existing", llm.get("api_key", ""))
        store.set_meta("active_model", {"provider_id": "existing", "model": llm["model"]})
    store.set_meta("model_profiles_migrated", True)


def migrate_single_model(store):
    """Collapse the role split onto one model, once, without reversing itself.

    Measured before this ran: ``active_model`` named gemini-4-argon while all six
    roles named gemini-3.8-flash, so the settings page and the work disagreed and
    neither was wrong on its own terms. ``active_model`` wins because it is what
    the selector has always shown and what a new conversation binds to.

    The roles key is left in place, unread. A rollback to an older tag then finds
    its assignments intact instead of discovering that the migration ate them,
    and no code path here writes to it again. An empty ``active_model`` with a
    populated role map falls back to the first assigned role so an installation
    that only ever used roles does not end up unconfigured.
    """
    if store.get_meta("model_roles_collapsed_v1"):
        return
    active = store.get_meta("active_model")
    if not active:
        roles = store.get_meta("model_roles") or {}
        first = next((roles[key] for key in ("chat", "feed", "team") if isinstance(roles.get(key), dict)), None)
        if first:
            store.set_meta("active_model", first)
    store.set_meta("model_roles_collapsed_v1", True)


def profiles_view(store):
    providers = [{**{k: copy.deepcopy(v) for k, v in row.items() if k != "api_key"}, "api_key_set": bool(store.get_secret("provider_" + row["id"]))}
                 for row in store.list("model_profiles")]
    # ``roles`` and ``resolved_roles`` are kept as a compatibility shim: the
    # assistant still reads `resolved_roles.chat` to label the conversation's
    # model. They now all report the same selection, which is the truth.
    active = store.get_meta("active_model")
    return {"providers": providers, "active": active,
            "roles": {}, "resolved_roles": {role: copy.deepcopy(active) for role in ("chat", "team")},
            "background": background_flags(store),
            "context": server_context()}


def server_context():
    """Read-only facts the 配置 page shows: port, database, build.

    Nothing here is editable from the browser. A port change needs a restart the
    launcher has to coordinate, and moving the database without its ``.key``
    makes every stored credential unreadable, so both are displayed and neither
    is a form field.
    """
    from .build_info import BUILD_ID, VERSION, listen_port
    from pathlib import Path
    import os
    data_dir = Path(os.environ.get("TOUCHLINE_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
    database = data_dir / "touchline.sqlite3"
    key = database.with_suffix(".key")
    try:
        size = database.stat().st_size if database.is_file() else 0
    except OSError:
        size = 0
    return {"version": VERSION, "build_id": BUILD_ID[:12],
            "port": listen_port(), "data_dir": str(data_dir),
            "database": {"path": str(database), "exists": database.is_file(), "bytes": size,
                         "key_present": key.is_file()}}


def effective_config(store, provider_id, model):
    profile = store.get("model_profiles", provider_id)
    if not profile:
        raise AgentFailure("所选模型服务已移除，请重新选择模型。")
    selected = next((m for m in profile["models"] if m["id"] == model), None)
    if not selected:
        raise AgentFailure("所选模型已不在该服务中，请重新选择模型。")
    llm = copy.deepcopy(store.settings()["llm"])
    llm.update(base_url=profile["base_url"], protocol=profile["protocol"], model=model, api_key=store.get_secret("provider_" + provider_id),
               _provider_id=provider_id)
    if selected.get("max_output_tokens"):
        llm["max_output_tokens"] = selected["max_output_tokens"]
    # Model-level parameters travel with the model, and only when the model
    # states them: an absent key means the request omits it rather than sending
    # a default the provider would reject.
    for key in ("temperature", "send_temperature", "reasoning_effort", "thinking_enabled", "timeout_seconds"):
        if key in selected:
            llm[key] = selected[key]
    llm["context_window"] = selected.get("context_window")
    return llm


def resolve_run_settings(store, run):
    settings = copy.deepcopy(store.settings())
    if run.get("provider_id"):
        try:
            settings["llm"] = effective_config(store, run["provider_id"], run.get("model") or run.get("model_id"))
        except AgentFailure:
            # The provider or model was removed after this run was created. The
            # active model is the only remaining choice; failing here would strand
            # a task whose conversation is otherwise valid.
            active = role_selection(store)
            settings["llm"] = effective_config(store, active["provider_id"], active["model"]) if active else {}
    elif not run.get("model"):
        selected = role_selection(store)
        if selected:
            settings["llm"] = effective_config(store, selected["provider_id"], selected["model"])
    return settings


def select_model(store, provider_id, model):
    try:
        config = effective_config(store, provider_id, model)
        endpoint_url(config)
    except AgentFailure as exc:
        raise HTTPException(400, str(exc)) from None
    store.save_settings({"llm": {key: config.get(key) for key in ("base_url", "protocol", "model", "max_output_tokens") if config.get(key) is not None}})
    # Empty key is intentional for a keyless local provider. The legacy setter
    # preserves empty keys on same-origin edits, so explicitly clear it here.
    if not config["api_key"]:
        store.save_settings({"llm": {"clear_api_key": True}})
    store.set_meta("active_model", {"provider_id": provider_id, "model": model})
    return profiles_view(store)


def save_profile(store, value: ProviderInput, *, update=False):
    previous = store.get("model_profiles", value.id)
    if update and not previous:
        raise HTTPException(404, "模型服务不存在。")
    if not update and previous:
        raise HTTPException(409, "这个服务商已存在。请编辑已有配置，或通过“添加服务商”新建一份配置。")
    data = value.model_dump(exclude_none=True, exclude={"api_key", "clear_api_key"})
    data["created_at"] = (previous or {}).get("created_at") or datetime.now(timezone.utc).isoformat()
    try:
        endpoint_url({**data, "model": data["models"][0]["id"]})
    except AgentFailure as exc:
        raise HTTPException(422, str(exc)) from None
    key_name = "provider_" + value.id
    changed_origin = previous and provider_origin(previous["base_url"]) != provider_origin(value.base_url)
    # Never send a saved credential to another origin simply because a form
    # was edited; keyless local providers remain supported.
    if value.clear_api_key or (changed_origin and not value.api_key):
        store.set_secret(key_name, None)
    elif value.api_key:
        store.set_secret(key_name, value.api_key)
    store.put("model_profiles", data)
    # The role map is no longer read or written. An edit that removed the model a
    # role pointed at used to rewrite that role to the provider's first model; with
    # one model there is nothing to keep in step, and touching the key would make a
    # rollback see assignments this build invented.
    active = store.get_meta("active_model")
    if not active:
        return select_model(store, value.id, data["models"][0]["id"])
    if active.get("provider_id") == value.id:
        chosen = active["model"] if any(m["id"] == active["model"] for m in data["models"]) else data["models"][0]["id"]
        return select_model(store, value.id, chosen)
    return profiles_view(store)


def _first_int(row, *names):
    """The first positive integer among these keys, including a nested path.

    A key written as a tuple is a path into a nested object (``("limit",
    "context")``), because providers disagree about whether a limit is a top-level
    field or lives under an envelope. Values below the model's own floor are
    discarded rather than clamped: a provider that reports 0 means "unstated".
    """
    for name in names:
        value = row
        for part in (name if isinstance(name, tuple) else (name,)):
            value = value.get(part) if isinstance(value, dict) else None
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number >= 256:
            return number
    return None


async def discover_models(store, payload: DiscoverInput):
    parsed = urlsplit(payload.base_url)
    path = parsed.path.rstrip("/") or "/v1"
    for suffix in PROTOCOLS.values():
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    url = urlunsplit((parsed.scheme, parsed.netloc, path + "/models", "", ""))
    key = payload.api_key or ""
    if not key and payload.provider_id:
        saved = store.get("model_profiles", payload.provider_id)
        if saved and provider_origin(saved["base_url"]) == provider_origin(payload.base_url) and saved["protocol"] == payload.protocol:
            key = store.get_secret("provider_" + payload.provider_id)
    headers = {"Accept": "application/json"}
    if payload.protocol == "anthropic_messages":
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
    elif key:
        headers["Authorization"] = "Bearer " + key
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=store.settings()["llm"].get("use_env_proxy", True)) as client:
            async with client.stream("GET", url, headers=headers) as response:
                parts, size = [], 0
                async for part in response.aiter_bytes():
                    size += len(part)
                    if size > 2_000_000:
                        raise HTTPException(502, "模型列表过大，请手动添加模型ID。")
                    parts.append(part)
                body = b"".join(parts)
                if response.status_code != 200:
                    raise HTTPException(502, str(http_failure(response.status_code, body)))
                import json
                data = json.loads(body)
                if not isinstance(data, (dict, list)):
                    raise ValueError()
                rows = data if isinstance(data, list) else data.get("data", data.get("models", []))
                if not isinstance(rows, list):
                    raise ValueError()
                models = {}
                for row in rows[:1000]:
                    identity = row if isinstance(row, str) else row.get("id") or row.get("name") if isinstance(row, dict) else None
                    if not isinstance(identity, str) or not identity.strip() or len(identity) > 200 or any(ord(c) < 32 for c in identity):
                        continue
                    name = row.get("display_name") or row.get("displayName") or identity if isinstance(row, dict) else identity
                    entry = {"id": identity, "name": str(name)[:200]}
                    # A provider that already states its limits saves the reader
                    # from typing them and from typing them wrong. Names differ by
                    # vendor; the DeepSeek Harness discovery reads the same set.
                    if isinstance(row, dict):
                        window = _first_int(row, "context_window", "context_length", "max_input_tokens",
                                            ("limit", "context"))
                        ceiling = _first_int(row, "max_output_tokens", "max_completion_tokens", "output_token_limit",
                                             ("top_provider", "max_completion_tokens"))
                        if window:
                            entry["context_window"] = window
                        if ceiling:
                            entry["max_output_tokens"] = ceiling
                    models[identity] = entry
                return {"models": list(models.values())}
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise HTTPException(502, "暂时无法获取模型列表；可以直接填写服务商提供的模型ID。") from None


def register_routes(app, store):
    migrate_legacy(store)
    migrate_single_model(store)

    @app.get("/api/model-providers")
    async def listing():
        return profiles_view(store)

    @app.post("/api/model-providers")
    async def create(value: ProviderInput):
        return {**save_profile(store, value), "saved_provider_id": value.id}

    @app.put("/api/model-providers/{identifier}")
    async def edit(identifier: str, value: ProviderInput):
        if identifier != value.id:
            raise HTTPException(400, "服务商ID不能修改，请新增服务商。")
        return save_profile(store, value, update=True)

    @app.delete("/api/model-providers/{identifier}")
    async def remove(identifier: str):
        if not store.get("model_profiles", identifier):
            raise HTTPException(404, "模型服务不存在。")
        store.delete("model_profiles", identifier)
        store.set_secret("provider_" + identifier, None)
        if (store.get_meta("active_model") or {}).get("provider_id") == identifier:
            store.set_meta("active_model", None)
            store.save_settings({"llm": {"base_url": "", "model": "", "clear_api_key": True}})
        return profiles_view(store)

    @app.post("/api/models/select")
    async def select(value: SelectInput):
        return select_model(store, value.provider_id, value.model)

    @app.post("/api/models/discover")
    async def discover(value: DiscoverInput):
        return await discover_models(store, value)

    @app.put("/api/models/background")
    async def background(value: BackgroundInput):
        """Turn the two unattended model passes on or off.

        Separate from the model itself on purpose: the model is one choice the
        reader makes once, while paying for a photo-planning call every time a
        team page opens is a different decision that deserves its own answer.
        """
        store.set_meta("background_model_tasks", {"photos": value.photos is True, "review": value.review is True})
        return profiles_view(store)

    @app.post("/api/models/test")
    async def test(value: TestProviderInput):
        """Probe a connection from the form, before anything is saved.

        The old endpoint resolved the *saved* profile, so finding out that a key
        was wrong required saving it first — and saving may overwrite a working
        key. This builds the config in memory from what the form currently shows,
        borrowing the saved secret only when the origin and protocol still match,
        which is the same rule ``save_profile`` uses to decide whether a stored
        credential may travel to a new address.
        """
        from .agent import test_connection
        from .model_harness import runtime_harness
        body = value.model_dump(exclude_none=True)
        api_key = body.pop("api_key", "")
        models = body.pop("models", [])
        chosen = next((row for row in models if row.get("id") == body["model"]), {})
        if not api_key and value.provider_id:
            saved = store.get("model_profiles", value.provider_id)
            if (saved and provider_origin(saved["base_url"]) == provider_origin(body["base_url"])
                    and saved.get("protocol") == body["protocol"]):
                api_key = store.get_secret("provider_" + value.provider_id)
        config = {**body, "api_key": api_key}
        for key in ("temperature", "send_temperature", "reasoning_effort", "thinking_enabled",
                    "timeout_seconds", "max_output_tokens", "context_window"):
            if key in chosen:
                config[key] = chosen[key]
        try:
            endpoint_url(config)
        except AgentFailure as exc:
            raise HTTPException(400, str(exc)) from None
        result = await test_connection(config, dispatcher=runtime_harness(store))
        # A model that answers and refuses to call a tool is still usable for
        # conversation. Tool support is reported, not required: failing the whole
        # probe on stage three hid working models behind a red banner.
        answers = [item for item in result["checks"] if item["stage"] in {"connection", "generation"}]
        result["usable"] = bool(answers) and all(item["ok"] for item in answers)
        if api_key:
            result["message"] = result.get("message", "").replace(api_key, "[密钥已隐藏]")
        return result
