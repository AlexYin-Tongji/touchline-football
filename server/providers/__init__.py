"""Registered information sources.

Import the provider modules for their registration side effect. The registry is
what the tool layer queries; no caller should reach an adapter by name.
"""
from __future__ import annotations

from ..provenance import SourceTier
from .base import (Answer, BaseProvider, Capability, Cost, Latency, ProviderError, Paced,
                   Source, adapter, configure_credentials, merge_answers, paced, provider,
                   register_provider, reset, restore, snapshot, source, sources)

__all__ = ["Answer", "BaseProvider", "Capability", "Cost", "Latency", "ProviderError", "Paced",
           "Source", "SourceTier", "adapter", "configure_credentials", "load", "merge_answers",
           "paced", "provider", "register_provider", "reset", "restore", "snapshot", "source",
           "sources"]


# Modules that declare providers. Importing one runs its registration, so the
# registry is populated by importing rather than by whoever happens to reach an
# adapter first. A provider missing from this tuple is simply not offered.
_MODULES = ("espn", "portraits", "reddit", "fan_feeds")


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
