"""Small provider catalog, write-only credentials, and per-conversation models."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .ingestion import normalize_url
from .provider import PROTOCOLS, AgentFailure, endpoint_url, http_failure
from .storage import provider_origin


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModelEntry(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field("", max_length=200)
    max_output_tokens: int | None = Field(None, ge=256, le=16000)

    @field_validator("id", "name")
    @classmethod
    def clean(cls, value):
        if any(ord(c) < 32 for c in value):
            raise ValueError("模型字段不能包含控制字符")
        return value.strip()


class ProviderInput(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=8, max_length=4096)
    protocol: str = "chat_completions"
    api_key: str | None = Field(None, max_length=8192)
    clear_api_key: bool = False
    models: list[ModelEntry] = Field(min_length=1, max_length=100)

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


class TestInput(StrictModel):
    model: str = Field(min_length=1, max_length=200)


def migrate_legacy(store):
    if store.get_meta("model_profiles_migrated"):
        return
    llm = store.settings()["llm"]
    if llm.get("base_url") and llm.get("model") and not store.list("model_profiles"):
        profile = {"id": "existing", "name": "现有模型服务", "base_url": llm["base_url"], "protocol": llm.get("protocol", "chat_completions"),
                   "models": [{"id": llm["model"], "name": llm["model"], "max_output_tokens": llm.get("max_output_tokens", 3000)}],
                   "created_at": datetime.now(timezone.utc).isoformat()}
        store.put("model_profiles", profile)
        store.set_secret("provider_existing", llm.get("api_key", ""))
        store.set_meta("active_model", {"provider_id": "existing", "model": llm["model"]})
    store.set_meta("model_profiles_migrated", True)


def profiles_view(store):
    providers = [{**{k: copy.deepcopy(v) for k, v in row.items() if k != "api_key"}, "api_key_set": bool(store.get_secret("provider_" + row["id"]))}
                 for row in store.list("model_profiles")]
    return {"providers": providers, "active": store.get_meta("active_model")}


def effective_config(store, provider_id, model):
    profile = store.get("model_profiles", provider_id)
    if not profile:
        raise AgentFailure("所选模型服务已移除，请重新选择模型。")
    selected = next((m for m in profile["models"] if m["id"] == model), None)
    if not selected:
        raise AgentFailure("所选模型已不在该服务中，请重新选择模型。")
    llm = copy.deepcopy(store.settings()["llm"])
    llm.update(base_url=profile["base_url"], protocol=profile["protocol"], model=model, api_key=store.get_secret("provider_" + provider_id))
    if selected.get("max_output_tokens"):
        llm["max_output_tokens"] = selected["max_output_tokens"]
    return llm


def resolve_run_settings(store, run):
    settings = copy.deepcopy(store.settings())
    if run.get("provider_id"):
        settings["llm"] = effective_config(store, run["provider_id"], run.get("model") or run.get("model_id"))
    return settings


def select_model(store, provider_id, model):
    try:
        config = effective_config(store, provider_id, model)
        endpoint_url(config)
    except AgentFailure as exc:
        raise HTTPException(400, str(exc)) from None
    store.save_settings({"llm": {key: config[key] for key in ("base_url", "protocol", "model", "api_key", "max_output_tokens")},})
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
        raise HTTPException(409, "这个服务商ID已经存在，请使用其他ID。")
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
    active = store.get_meta("active_model")
    if not active:
        return select_model(store, value.id, data["models"][0]["id"])
    if active.get("provider_id") == value.id:
        chosen = active["model"] if any(m["id"] == active["model"] for m in data["models"]) else data["models"][0]["id"]
        return select_model(store, value.id, chosen)
    return profiles_view(store)


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
                    models[identity] = {"id": identity, "name": str(name)[:200]}
                return {"models": list(models.values())}
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise HTTPException(502, "暂时无法获取模型列表；可以直接填写服务商提供的模型ID。") from None


def register_routes(app, store):
    migrate_legacy(store)

    @app.get("/api/model-providers")
    async def listing():
        return profiles_view(store)

    @app.post("/api/model-providers")
    async def create(value: ProviderInput):
        return save_profile(store, value)

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

    @app.post("/api/model-providers/{identifier}/test")
    async def test(identifier: str, value: TestInput):
        from .agent import test_connection
        try:
            config = effective_config(store, identifier, value.model)
        except AgentFailure as exc:
            raise HTTPException(400, str(exc)) from None
        return await test_connection(config)
