"""One shape for every information source.

Before this module each source was wired in four places: a host constant in
``news_sources``, a branch in ``research.readable_publisher_entry``, a catalog
entry, and a domain added to ``TRUSTED_HOSTS``. Adding a source therefore meant
editing publisher-specific code, and the tier a source spoke with was implied by
where its branch happened to live.

Here a source declares what it is and what it can answer, and the registry
resolves that into the tools one task may use. Nothing about a provider decides
what the product says about it: wording and admission come from
``provenance``, so a community source cannot promote itself by declaring a
stronger tier.

Two rules hold for every provider:

* It returns claims or references, never prose. Presentation is a later layer's
  job, and a provider that writes sentences cannot be merged with another.
* It reports its own gaps. "No results" and "the request failed" are different
  answers, and a caller that cannot tell them apart will show an outage as an
  absence of information.
"""
from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

from .. import provenance as prov
from ..provenance import ProvenanceError, SourceTier


class ProviderError(RuntimeError):
    """A provider-level failure that the caller must not read as emptiness."""

    def __init__(self, message, *, retryable=False, code="provider_error"):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class Capability(str, Enum):
    """What a provider can be asked for, independent of who it is."""

    NEWS = "news"              # current reporting about a club
    PERSON = "person"          # player identity and biographics
    SQUAD = "squad"            # who is in a squad right now
    FIXTURE = "fixture"        # matches and results
    STANDINGS = "standings"    # league tables
    IMAGE = "image"            # portraits
    OPINION = "opinion"        # supporters, forums, previews


class Cost(str, Enum):
    """Roughly what one call spends, for the scheduler's budget gate."""

    FREE = "free"              # local or already cached
    LIGHT = "light"            # one plain HTTP GET, no key
    QUOTA = "quota"            # consumes a metered allowance
    KEY = "key"                # requires a stored credential


class Latency(str, Enum):
    FAST = "fast"              # under a second, from cache or disk
    NORMAL = "normal"
    SLOW = "slow"              # seconds, and may need pacing


@dataclass(frozen=True)
class Source:
    """A reviewed source, registered once with its tier and domains."""

    id: str
    name: str
    tier: SourceTier
    domains: tuple
    capabilities: frozenset
    cost: Cost = Cost.LIGHT
    latency: Latency = Latency.NORMAL
    note: str = ""
    requires_key: str = ""      # named entry in the credential store, if any
    # A named credential this source *uses when present* but does not require.
    # TheSportsDB reads on the free key 3 and answers with substituted sample
    # data; a paid key raises the real limits without being a precondition for
    # registering the source. ``source_config`` lists both kinds so the settings
    # page can offer a field for either.
    optional_key: str = ""
    reviewed_at: str = ""

    def __post_init__(self):
        if not self.domains:
            raise ProvenanceError(f"来源 {self.id} 必须有域名。")
        if not isinstance(self.tier, SourceTier):
            object.__setattr__(self, "tier", SourceTier(str(self.tier)))
        object.__setattr__(self, "capabilities", frozenset(
            item if isinstance(item, Capability) else Capability(str(item)) for item in self.capabilities))


def _capability(value):
    """Accept a Capability member or its name, and refuse anything else."""
    if isinstance(value, Capability):
        return value
    try:
        return Capability(str(value))
    except ValueError:
        raise ProviderError(f"未知能力：{value}。") from None


@dataclass
class Answer:
    """The one envelope every provider returns.

    ``items`` are claims or provider references; ``gaps`` explains what the
    provider could not deliver and why. A caller merges envelopes; it never
    parses provider-specific shapes.
    """

    source_id: str
    tier: SourceTier
    capability: Capability
    items: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    cached: bool = False
    spent: Cost = Cost.FREE
    notices: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.gaps or bool(self.items)

    def safe(self):
        return {"source_id": self.source_id, "tier": self.tier.value, "capability": self.capability.value,
                "items": copy.deepcopy(self.items), "gaps": list(self.gaps), "cached": self.cached,
                "spent": self.spent.value, "notices": list(self.notices)}


# --- adapters ---------------------------------------------------------------

_ADAPTERS = {}
_REGISTERED = {}
_THROTTLE = {}
# Registrations made by importing the shipped provider modules. A test that
# registers its own sources restores this baseline afterwards instead of wiping
# the registry, which would leave later tests unable to see the real ones.
_BUILTIN = None


def snapshot():
    """The current registrations, for a test that is about to change them."""
    return (dict(_ADAPTERS), dict(_REGISTERED), prov.snapshot_registry())


def restore(state):
    """Put back a snapshot taken by :func:`snapshot`."""
    adapters, registered_sources, registry = state
    _ADAPTERS.clear()
    _ADAPTERS.update(adapters)
    _REGISTERED.clear()
    _REGISTERED.update(registered_sources)
    prov.restore_registry(registry)


def reset():
    """Drop every registration, including the shipped providers.

    Tests that only add their own sources should use :func:`snapshot` and
    :func:`restore`; this is for cases that need a genuinely empty registry.
    """
    _ADAPTERS.clear()
    _REGISTERED.clear()
    _THROTTLE.clear()
    from .. import provenance as prov_module
    prov_module.clear_registry()


def provider(source, capabilities, *, cost=Cost.LIGHT, latency=Latency.NORMAL,
             requires_key="", optional_key="", note="", reviewed_at=""):
    """Declare one provider and its source, and return the adapter class.

    Registration is the composition step: it both tells ``provenance`` which
    tier this host may speak with, and records what the registry may ask for.
    """
    def decorate(cls):
        if isinstance(source, Source):
            declared = source
        else:
            fields = dict(source or {})
            # Explicit decorator keywords win over the mapping, so a provider
            # may keep its descriptive fields together and still override one.
            fields.update({key: value for key, value in
                           (("cost", cost), ("latency", latency), ("note", note), ("reviewed_at", reviewed_at))
                           if value})
            fields.setdefault("capabilities", capabilities)
            if requires_key:
                fields["requires_key"] = requires_key
            if optional_key:
                fields["optional_key"] = optional_key
            declared = Source(**fields)
        register_provider(declared)
        cls.source = declared
        cls.pacing = paced(declared.id, getattr(cls, "min_interval", 0.0))
        _ADAPTERS[declared.id] = cls
        return cls
    return decorate


def sources(*, capability=None, tier=None, reachable=True):
    """Every registered source, filtered by what the caller can use right now.

    ``reachable`` drops sources whose credential is absent, so a task never
    advertises a capability it cannot actually fulfil.
    """
    rows = []
    for entry in _REGISTERED.values():
        if capability is not None and _capability(capability) not in entry.capabilities:
            continue
        if tier is not None and entry.tier is not SourceTier(str(tier)):
            continue
        if reachable and entry.requires_key and not _has_credential(entry.requires_key):
            continue
        rows.append(copy.deepcopy(entry))
    rows.sort(key=lambda item: (_TIER_RANK.get(item.tier, 9), item.id))
    return rows


_CREDENTIAL_LOOKUP = None


def configure_credentials(lookup):
    """Point the registry at the credential store, once, at startup."""
    global _CREDENTIAL_LOOKUP
    _CREDENTIAL_LOOKUP = lookup


def _has_credential(name):
    if _CREDENTIAL_LOOKUP is None:
        return False
    try:
        return bool(_CREDENTIAL_LOOKUP(name))
    except Exception:
        return False


_TIER_RANK = {SourceTier.OFFICIAL: 0, SourceTier.DATASET: 1, SourceTier.PRESS: 2,
              SourceTier.COMMUNITY: 3, SourceTier.DERIVED: 4}


def source(source_id):
    return copy.deepcopy(_REGISTERED.get(source_id))


def register_provider(declared):
    """Register an already-built Source. Adapters use the decorator instead."""
    if not isinstance(declared, Source):
        raise ProvenanceError("来源声明必须是 Source。")
    for domain in declared.domains:
        prov.register(declared.id, domain, declared.tier, name=declared.name,
                      kinds=[capability.value for capability in declared.capabilities],
                      note=declared.note)
    _REGISTERED[declared.id] = declared
    return declared


def adapter(source_id):
    cls = _ADAPTERS.get(source_id)
    if cls is None:
        raise ProviderError(f"来源 {source_id} 未注册。")
    return cls


def reset():
    """Temporarily kept for callers that want an empty registry.

    Prefer :func:`snapshot` and :func:`restore`: wiping the registry hides the
    shipped providers from every test that runs afterwards.
    """
    _ADAPTERS.clear()
    _REGISTERED.clear()
    _THROTTLE.clear()
    from .. import provenance as prov_module
    prov_module.clear_registry()


# --- pacing ----------------------------------------------------------------
#
# A free tier that throttles bursts must be paced by the registry rather than
# by each adapter, or two adapters sharing a quota will each run at full rate.

class Paced:
    """Minimum interval between calls to one provider, shared across callers."""

    def __init__(self, source_id, min_interval):
        self.source_id = source_id
        self.min_interval = float(min_interval)
        self.enabled = True

    def disable(self):
        """Stop waiting. Only a test should do this, and it says so explicitly.

        Without it, every provider test pays its provider's real interval, and
        a suite with five metered providers takes minutes for no extra
        coverage.
        """
        self.enabled = False

    async def wait(self, store=None, *, loop=None):
        if not self.enabled or self.min_interval <= 0:
            return
        key = self.source_id
        now = time.monotonic()
        last = _THROTTLE.get(key, 0.0)
        gap = self.min_interval - (now - last)
        if gap > 0:
            await asyncio.sleep(gap)
        _THROTTLE[key] = time.monotonic()


def unpaced():
    """Turn off pacing for every registered adapter, for a test suite."""
    for cls in _ADAPTERS.values():
        getattr(cls, "pacing", None) and cls.pacing.disable()


def paced(source_id, min_interval):
    return Paced(source_id, min_interval)


class BaseProvider:
    """Common behaviour: answer shape, gap reporting, and honest caching.

    Subclasses implement the capability methods they declare. A method that is
    not implemented raises rather than returning an empty list, because an empty
    answer is indistinguishable from "the source has nothing" and that
    difference is exactly what a reader needs.
    """

    source: Source

    def __init__(self, store=None, settings=None):
        self.store = store
        self.settings = settings or {}

    def envelope(self, capability, items=None, gaps=None, *, cached=False, spent=None, notices=None):
        return Answer(source_id=self.source.id, tier=self.source.tier, capability=_capability(capability),
                      items=list(items or []), gaps=list(gaps or []), cached=cached,
                      spent=spent or self.source.cost, notices=list(notices or []))

    def unavailable(self, capability, message, *, retryable=False):
        """A gap, never an empty success: an outage must not read as absence."""
        return self.envelope(capability, gaps=[{"source_id": self.source.id, "message": message,
                                                "retryable": bool(retryable)}])

    def credentials(self):
        name = self.source.requires_key
        if not name:
            return ""
        if self.store is None or not hasattr(self.store, "get_secret"):
            raise ProviderError(f"{self.source.name} 需要凭据，但当前没有可用的密钥存储。",
                                code="missing_credential")
        value = self.store.get_secret(name)
        if not value:
            raise ProviderError(f"{self.source.name} 尚未配置密钥。", code="missing_credential")
        return value


def merge_answers(answers):
    """One envelope from many, preserving every source's own attribution."""
    answers = [answer for answer in answers if answer is not None]
    if not answers:
        return Answer(source_id="", tier=SourceTier.DERIVED, capability=Capability.NEWS)
    strongest = min(answers, key=lambda item: _TIER_RANK.get(item.tier, 9))
    return Answer(
        source_id=strongest.source_id, tier=strongest.tier, capability=strongest.capability,
        items=[item for answer in answers for item in answer.items],
        gaps=[gap for answer in answers for gap in answer.gaps],
        cached=all(answer.cached for answer in answers),
        spent=max(answers, key=lambda item: list(Cost).index(item.spent)).spent,
        notices=[notice for answer in answers for notice in answer.notices],
    )
