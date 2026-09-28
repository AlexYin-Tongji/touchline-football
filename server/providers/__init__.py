"""Registered information sources.

Import the provider modules for their registration side effect. The registry is
what the tool layer queries; no caller should reach an adapter by name.
"""
from __future__ import annotations

from ..provenance import SourceTier
from .base import (Answer, BaseProvider, Capability, Cost, Latency, ProviderError, Paced,
                   Source, adapter, configure_credentials, merge_answers, paced, provider,
                   register_provider, reset, source, sources)

__all__ = ["Answer", "BaseProvider", "Capability", "Cost", "Latency", "ProviderError", "Paced",
           "Source", "SourceTier", "adapter", "configure_credentials", "merge_answers", "paced",
           "provider", "register_provider", "reset", "source", "sources"]
