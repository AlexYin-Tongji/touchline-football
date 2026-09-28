"""Registered information sources.

Import the provider modules for their registration side effect. The registry is
what the tool layer queries; no caller should reach an adapter by name.
"""
from __future__ import annotations

from ..provenance import SourceTier
from .base import (Answer, BaseProvider, Capability, Cost, Latency, ProviderError, Paced,
                   Source, adapter, configure_credentials, merge_answers, paced, provider,
                   register_provider, reset, restore, snapshot, source, sources, unpaced)

__all__ = ["Answer", "BaseProvider", "Capability", "Cost", "Latency", "ProviderError", "Paced",
           "Source", "SourceTier", "adapter", "configure_credentials", "load", "merge_answers",
           "paced", "provider", "register_provider", "reset", "restore", "snapshot", "source",
           "sources", "unpaced", "query"]


# Modules that declare providers. Importing one runs its registration, so the
# registry is populated by importing rather than by whoever happens to reach an
# adapter first. A provider missing from this tuple is simply not offered.
_MODULES = ("espn", "portraits", "reddit", "fan_feeds", "football_data", "api_football")


async def query(source_id, capability, *, store, **scope):
    """Resolve a registered capability and invoke the corresponding adapter.

    A provider cannot gain an undeclared capability by adding a method, and
    keyed sources are checked against this Store at the call boundary.
    """
    requested = capability if isinstance(capability, Capability) else Capability(str(capability))
    declared = source(source_id)
    if declared is None or requested not in declared.capabilities:
        raise ProviderError(f"来源 {source_id} 未提供 {requested.value} 能力。", code="unsupported_capability")
    if declared.requires_key and (store is None or not store.get_secret(declared.requires_key)):
        return Answer(source_id=declared.id, tier=declared.tier, capability=requested,
                      gaps=[{"source_id": declared.id, "code": "missing_credential",
                             "message": f"来源 {source_id} 尚未配置密钥。", "retryable": False}],
                      spent=Cost.FREE)
    instance = adapter(source_id)(store=store)
    method_name = "fixtures" if requested is Capability.FIXTURE else requested.value
    method = getattr(instance, method_name, None)
    if method is None:
        raise ProviderError(f"来源 {source_id} 尚未实现 {requested.value} 能力。", code="missing_adapter")
    result = method(**scope)
    if hasattr(result, "__await__"):
        result = await result
    if not isinstance(result, Answer):
        raise ProviderError(f"来源 {source_id} 返回格式不正确。", code="invalid_answer")
    return result


def load():
    """Import every shipped provider module so its registrations take effect."""
    import importlib
    loaded = []
    for name in _MODULES:
        try:
            loaded.append(importlib.import_module(f"{__name__}.{name}").__name__)
        except Exception as exc:
            raise ImportError(f"来源模块 {name} 加载失败：{exc}") from exc
    return loaded


# Register the shipped providers on first import. A module that is never
# imported cannot answer anything, and discovering that at query time would
# look like a source that returned nothing.
load()
