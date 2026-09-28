"""Private source credential management; operational state stays server-side."""
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from . import providers


class CredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_key: SecretStr = Field(min_length=1, max_length=8192)


def register_routes(app, store):
    def keyed_source(identity):
        row = next((row for row in providers.sources(reachable=False) if row.id == identity and row.requires_key), None)
        if row is None:
            raise HTTPException(404, "没有此凭据来源。")
        return row

    def safe_source(row):
        configured = not row.requires_key or bool(store.get_secret(row.requires_key))
        result = {"id": row.id, "name": row.name, "configured": configured,
                  "requires_key": bool(row.requires_key), "capabilities": sorted(cap.value for cap in row.capabilities)}
        if row.id == "api_football":
            from .provider_quota import DailyQuota
            result["quota"] = DailyQuota(store, "api_football").status()
        return result

    @app.get("/api/data-sources")
    async def source_status():
        return {"sources": [safe_source(row) for row in providers.sources(reachable=False)]}

    @app.put("/api/data-sources/{identity}/credential")
    async def set_credential(identity: str, value: CredentialInput):
        row = keyed_source(identity)
        store.set_secret(row.requires_key, value.api_key.get_secret_value())
        return safe_source(row)

    @app.delete("/api/data-sources/{identity}/credential")
    async def clear_credential(identity: str):
        row = keyed_source(identity)
        store.set_secret(row.requires_key, None)
        return safe_source(row)
