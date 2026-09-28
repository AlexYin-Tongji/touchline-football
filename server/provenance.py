"""Source tiers, field-level claims and freshness.

Touchline integrates information; it does not certify facts. This module is the
one place that decides how a statement is *attributed*, how long it stays worth
trusting, and what happens when two sources disagree. It deliberately contains
no truth judgement: a claim is carried with its provenance and competing claims
are kept side by side rather than resolved.

Design rules (see docs/DESIGN_PRINCIPLES.md):

* A tier decides the wording used to relay a statement, never its admission.
  ``COMMUNITY`` is not a lesser ``PRESS``: it carries eyewitness and timing
  value the official channels never publish.
* ``corroboration`` orders community sources among themselves and is never
  allowed to promote one into a stronger tier.
* Two clocks are kept apart: ``fetched_at`` is when we retrieved a statement,
  ``observed_at`` is when the source says it happened. Retrieval time must not
  make old news look new.
* Freshness is derived from how fast the underlying fact can change, including
  how close a fixture is. A squad list is worth rechecking twelve times more
  often in the last week of a transfer window than mid-season.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from urllib.parse import urlsplit


class ProvenanceError(ValueError):
    """A claim or tier assignment that the store must not accept."""


class SourceTier(str, Enum):
    """How a statement is relayed. Ordering here is presentational only."""

    OFFICIAL = "official"      # the club, league or governing body itself
    DATASET = "dataset"        # structured provider (ESPN, API-Football, ...)
    PRESS = "press"            # publisher with editorial responsibility
    COMMUNITY = "community"    # supporters, forums, personal accounts
    DERIVED = "derived"        # computed here from other claims


# Tiers a caller may hand to us. A model can never reach this function with a
# tier of its own choosing: every value must appear in this table, and the
# relays below are the only wording the product uses.
RELAY = {
    SourceTier.OFFICIAL: "官方",
    SourceTier.DATASET: "数据源",
    SourceTier.PRESS: "报道",
    SourceTier.COMMUNITY: "社区说法",
    SourceTier.DERIVED: "计算",
}

# How many independent community voices are needed before the relay wording
# mentions agreement. This changes the sentence, never the tier.
COMMUNITY_CORROBORATION_LEVELS = ((3, "多人提及"), (1, "有人提及"))


class Freshness(str, Enum):
    """How a stored value should be presented right now."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class Relation(str, Enum):
    """Which club relationship a statement is about.

    "Which club is this player at" has no single answer. A loaned player is at
    his loan club and contracted to his parent club at the same time, so a
    structured provider naming the parent and a community source naming the
    loan club are not disagreeing — they are answering two different questions.
    Collapsing them into one ``team_id`` is what made every cross-source club
    comparison look like a contradiction.

    A claim about club membership carries one of these, and claims that differ
    only by relation are kept apart rather than merged.
    """

    PARENT_CLUB = "parent_club"    # holds the contract
    CURRENT_CLUB = "current_club"  # where he actually plays, loans included
    FORMER_CLUB = "former_club"    # historical, never current
    YOUTH_CLUB = "youth_club"      # academy only, never a senior appearance

    @property
    def current(self):
        return self in {Relation.PARENT_CLUB, Relation.CURRENT_CLUB}


# Fields whose value identifies a club rather than a scalar. Two sources naming
# different clubs for one of these are only comparable when the relation agrees.
CLUB_FIELDS = frozenset({"club_id", "team_id", "club", "team"})


def _resolve_relation(claim):
    value = claim.value if isinstance(claim.value, dict) else {}
    raw = claim.derivation.get("relation") or value.get("relation") or Relation.CURRENT_CLUB.value
    try:
        return Relation(str(raw))
    except ValueError:
        return Relation.CURRENT_CLUB


# --- fresh information is worth rechecking soon; settled facts are not -------

HOUR = 3600
DAY = 24 * HOUR
YEAR = 365 * DAY

# Facts that do not move. Rechecking them spends a rate-limited request to learn
# nothing, and a quota spent here is a request the squad list does not get.
#
# These fields are also exempt from deadline-driven acceleration: a transfer
# window cannot make a birthday change faster, and letting it shorten their
# budget would put settled facts at the front of the review queue.
IMMUTABLE_FIELDS = frozenset({
    "birth_date", "nationality", "height_cm", "weight_kg", "position",
    "position_group", "name", "photo_url",
})

# Fields whose value really does turn over with the window and the fixture list.
VOLATILE_FIELDS = frozenset({
    "roster", "squad", "number", "availability", "injury", "lineup", "status",
    "transfer", "contract", "loan", "score", "result", "fixture", "schedule",
    "standings", "stats", "team_id",
})

TTL_BY_FIELD = {
    "birth_date": YEAR, "nationality": YEAR, "height_cm": YEAR,
    "weight_kg": YEAR, "photo_url": YEAR, "position": YEAR, "position_group": YEAR,
    "number": 30 * DAY, "name": YEAR, "bio": 30 * DAY, "stats": DAY,
    "team_id": DAY, "season_id": YEAR, "league_id": YEAR,
    "roster": DAY, "squad": DAY,
    "standings": DAY, "schedule": DAY, "fixture": DAY,
    "score": HOUR, "result": HOUR, "status": HOUR,
    "injury": 48 * HOUR, "availability": 48 * HOUR, "lineup": 12 * HOUR,
    "transfer": 12 * HOUR, "contract": 12 * HOUR, "loan": 12 * HOUR,
}
DEFAULT_TTL = DAY

# The transfer window is the one period where a squad list changes faster than
# its cache. Only the *months* a window opens can be a static default; the
# deadline day itself varies by league and season, so callers that know it pass
# ``window_end``. Guessing a deadline from the calendar would silently expire
# correctly-fetched squads on the wrong day.
TRANSFER_WINDOW_MONTHS = frozenset({1, 2, 6, 7, 8})
TRANSFER_WINDOW_TTL = 12 * HOUR
TRANSFER_DEADLINE_DAYS = 7
TRANSFER_DEADLINE_TTL = 6 * HOUR
MATCH_DAY_TTL = 2 * HOUR
PRE_MATCH_ACCELERATION_HOURS = 48


def _utc(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    else:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def iso(value):
    stamp = _utc(value)
    return stamp.isoformat() if stamp else None


def ttl_for(field_name, *, kickoff=None, now=None, window=None, window_end=None):
    """Seconds this field stays worth trusting, measured from ``now``.

    ``now`` is the moment the budget starts counting — normally the instant the
    value was retrieved, not the instant someone reads it. Passing the read time
    here would re-derive the budget every render and let a claim that was
    fetched inside a transfer window keep a window-sized budget forever.

    Three inputs raise urgency, and they compose the way a person would expect:
    a squad list in the last week of the transfer window two hours before
    kickoff is refreshed on a two-hour clock, not an annual one.
    """
    name = str(field_name or "")
    base = TTL_BY_FIELD.get(name, DEFAULT_TTL)
    if name in IMMUTABLE_FIELDS:
        # A settled fact has no urgency to compose: neither the window nor the
        # fixture list changes how fast a birthday moves.
        return base
    if name in {"score", "result", "status"}:
        return base
    if window is True or (window is None and _in_transfer_window(now)):
        base = min(base, TRANSFER_WINDOW_TTL)
    if window_end is not None:
        end = _utc(window_end)
        moment = _utc(now) or datetime.now(timezone.utc)
        if end is not None and timedelta(0) <= end - moment <= timedelta(days=TRANSFER_DEADLINE_DAYS):
            base = min(base, TRANSFER_DEADLINE_TTL)
    if kickoff is not None and name in {
            "roster", "squad", "availability", "injury", "lineup", "number", "status"}:
        start = _utc(kickoff)
        moment = _utc(now) or datetime.now(timezone.utc)
        if start is not None:
            remaining = (start - moment).total_seconds()
            if remaining <= 0 and remaining > -6 * HOUR:
                base = min(base, MATCH_DAY_TTL)
            elif 0 < remaining <= PRE_MATCH_ACCELERATION_HOURS * HOUR:
                base = min(base, MATCH_DAY_TTL)
    return base


def _in_transfer_window(now=None):
    moment = _utc(now) or datetime.now(timezone.utc)
    return moment.month in TRANSFER_WINDOW_MONTHS


@dataclass(frozen=True)
class Claim:
    """One attributed statement about one field.

    ``value`` is what the source said, unchanged. ``evidence_id`` points at the
    text we actually read, and is required whenever the claim came from a
    document. A claim with no source is not a claim.
    """

    field: str
    value: object
    source_id: str
    tier: SourceTier
    fetched_at: str = ""
    observed_at: str = ""
    ttl_seconds: int | None = None
    evidence_id: str = ""
    source_name: str = ""
    source_url: str = ""
    corroboration: int | None = None
    note: str = ""
    derivation: dict = field(default_factory=dict)

    def __post_init__(self):
        if not str(self.source_id or "").strip():
            raise ProvenanceError("主张必须标明来源。")
        if not str(self.field or "").strip():
            raise ProvenanceError("主张必须属于某个字段。")
        if not isinstance(self.tier, SourceTier):
            try:
                object.__setattr__(self, "tier", SourceTier(str(self.tier)))
            except ValueError:
                raise ProvenanceError("来源层级无效。") from None
        if self.ttl_seconds is None:
            # The field decides its own budget. Leaving a fixed default here
            # would give a birthday the same day-long budget as a score, and the
            # review queue would then chase settled facts.
            object.__setattr__(self, "ttl_seconds", ttl_for(self.field, now=self.fetched_at or None))
        elif not isinstance(self.ttl_seconds, int) or self.ttl_seconds < 0:
            raise ProvenanceError("新鲜度必须是秒数。")
        if self.corroboration is not None and (not isinstance(self.corroboration, int) or self.corroboration < 0):
            raise ProvenanceError("认同量必须是自然数。")

    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)

    @property
    def key(self) -> tuple:
        """Merging identity: field, normalised value, tier, and club relation.

        The relation joins the key only for club fields. Without it, a parent
        club and a loan club would merge into one "contradiction"; with it,
        they sit side by side as two true statements about different things.
        """
        relation = _resolve_relation(self).value if self.field in CLUB_FIELDS else ""
        return (self.field, _canonical(self.value), self.tier, relation)

    def safe(self) -> dict:
        """A copy carrying no internal-only structure."""
        return {
            "field": self.field, "value": copy.deepcopy(self.value), "source_id": self.source_id,
            "tier": self.tier.value, "fetched_at": self.fetched_at, "observed_at": self.observed_at,
            "ttl_seconds": self.ttl_seconds, "evidence_id": self.evidence_id,
            "source_name": self.source_name, "source_url": self.source_url,
            "corroboration": self.corroboration, "note": self.note,
            **({"derivation": copy.deepcopy(self.derivation)} if self.derivation else {}),
        }

    def age_seconds(self, now=None):
        stamp = _utc(self.fetched_at)
        if stamp is None:
            return None
        return ((_utc(now) or datetime.now(timezone.utc)) - stamp).total_seconds()

    def due_at(self):
        """When this value stops being worth showing without a fresh look.

        Anchored to ``fetched_at``, never to the moment of reading: the budget
        belongs to the retrieval, so a value fetched inside a transfer window
        does not carry a window-sized budget for the rest of its life.
        """
        stamp = _utc(self.fetched_at)
        if stamp is None or not self.ttl_seconds:
            return None
        return stamp + timedelta(seconds=self.ttl_seconds)

    def overdue_ratio(self, now=None):
        """How many times over its own budget this value is.

        Ordering the review queue by elapsed seconds would put a year-old birth
        date ahead of a month-old score. Ordering by this ratio puts the score
        first, which is what a person means by "most out of date".
        """
        due = self.due_at()
        if due is None:
            return None
        moment = _utc(now) or datetime.now(timezone.utc)
        elapsed = (moment - due).total_seconds()
        return elapsed / max(1, self.ttl_seconds) if elapsed > 0 else 0.0

    def freshness(self, now=None):
        age = self.age_seconds(now)
        if age is None:
            return Freshness.UNKNOWN
        # A future stamp is a clock disagreement, not freshness.
        if age < -300:
            return Freshness.UNKNOWN
        return Freshness.FRESH if age < max(0, self.ttl_seconds or 0) else Freshness.STALE

    def expired(self, now=None):
        return self.freshness(now) is Freshness.STALE

    def relay(self):
        """The sentence shape this claim is allowed to appear in.

        The tier decides the wording; corroboration can only add a qualifier to
        a community claim. Neither can promote the claim to another tier.
        """
        if self.tier is not SourceTier.COMMUNITY:
            return RELAY[self.tier]
        count = self.corroboration or 0
        for threshold, wording in COMMUNITY_CORROBORATION_LEVELS:
            if count >= threshold:
                return f"{RELAY[self.tier]}·{wording}"
        return RELAY[self.tier]


def _canonical(value):
    """Compare values without rewriting them.

    Only representation differences are normalised: whitespace, case of plain
    strings, and numeric types that describe the same number. Numbers that
    differ (183 vs 185) stay different — that disagreement is the point.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number) if number.is_integer() else number
    if isinstance(value, str):
        text = " ".join(value.split())
        if text.isdecimal():
            return int(text)
        return text.casefold() if text.isascii() else text
    if isinstance(value, (list, tuple)):
        return tuple(_canonical(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((str(key), _canonical(item)) for key, item in value.items()))
    return str(value)


def merge_claims(claims, *, now=None):
    """Group claims into field groups, keeping disagreements side by side.

    Claims only merge when the field, the normalised value *and* the tier agree.
    Crossing a tier boundary is never automatic: an official figure of 183 and a
    community figure of 183 came from different places and both stay visible.
    """
    groups = {}
    for claim in claims:
        if not isinstance(claim, Claim):
            claim = Claim(**claim) if isinstance(claim, dict) else None
        if claim is None:
            continue
        groups.setdefault(claim.key, []).append(claim)
    merged = []
    for key, members in groups.items():
        field, value, tier = key[0], key[1], key[2]
        relation = key[3] if len(key) > 3 else ""
        best = max(members, key=lambda item: (item.corroboration or 0, item.fetched_at or ""))
        merged.append(replace(
            best,
            corroboration=sum(item.corroboration or 0 for item in members) or None,
            evidence_id=best.evidence_id,
            note=best.note,
            derivation={
                # Keep every attribution even when the values agreed, so the UI
                # can say "three sources say this" instead of hiding agreement.
                "merged_from": sorted({item.source_id for item in members}),
                **({"relation": relation} if relation else {}),
            },
        ))
    return merged


def field_view(claims, field_name, *, now=None):
    """Everything known about one field, ordered for display.

    Stronger tiers first, then the fresher claim, then corroboration. Values
    that disagree are all returned: the caller shows them together and lets the
    reader decide.
    """
    order = {SourceTier.OFFICIAL: 0, SourceTier.DATASET: 1, SourceTier.PRESS: 2,
             SourceTier.COMMUNITY: 3, SourceTier.DERIVED: 4}
    rows = [claim if isinstance(claim, Claim) else Claim(**claim) for claim in claims
            if str(claim.get("field", claim.field if isinstance(claim, Claim) else "")) == field_name]
    rows.sort(key=lambda item: (order.get(item.tier, 9),
                                item.freshness(now) is Freshness.STALE,
                                -(item.corroboration or 0),
                                str(item.fetched_at or "")))
    return rows


def conflicts(claims, *, now=None):
    """Fields where the strongest claims disagree, listed without a verdict.

    Grouping follows the merge key, so a parent club and a loan club are never
    reported as a disagreement: they answer different questions about the same
    player and both are true.
    """
    by_key = {}
    for claim in merge_claims(claims, now=now):
        relation = _resolve_relation(claim).value if claim.field in CLUB_FIELDS else ""
        by_key.setdefault((claim.field, relation), []).append(claim)
    result = []
    for (name, relation), rows in sorted(by_key.items()):
        live = [row for row in rows if row.freshness(now) is not Freshness.STALE]
        if len({_canonical(row.value) for row in live}) < 2:
            continue
        result.append({"field": name, "relation": relation or None,
                       "claims": [row.safe() for row in field_view(live, name, now=now)]})
    return result


def derived(field_name, value, *, inputs, algorithm, now=None):
    """A value computed here. Always records what it was computed from."""
    return Claim(
        field=field_name, value=value, source_id="touchline", tier=SourceTier.DERIVED,
        fetched_at=iso(now) or datetime.now(timezone.utc).isoformat(),
        ttl_seconds=ttl_for(field_name, now=now),
        derivation={"algorithm": algorithm,
                    "inputs": [item.evidence_id or f"{item.source_id}:{item.field}" for item in inputs]},
    )


# --- registration -----------------------------------------------------------
#
# A domain may only claim a tier it has been registered with. This is what stops
# a community post from being relayed as an official announcement: the tier is
# looked up here, never taken from the caller.

_REGISTRY = {}
_TIER_ORDER = (SourceTier.OFFICIAL, SourceTier.DATASET, SourceTier.PRESS, SourceTier.COMMUNITY, SourceTier.DERIVED)


class UnregisteredSource(ProvenanceError):
    """Raised when a source asks for a tier it was never granted."""


def _host_keys(domain):
    """Registry keys a host declaration covers.

    A plain name covers only that host and its ``www.`` alias. A leading dot
    covers that host and every subdomain, which some providers genuinely need
    (Wikimedia serves API traffic from several). The distinction is explicit so
    a provider cannot cover a whole domain by accident.
    """
    host = str(domain or "").strip().lower()
    if not host:
        return []
    if host.startswith("."):
        base = host[1:]
        if "." not in base:
            raise ProvenanceError("子域授权必须写明根域名。")
        return ["." + base, base]
    if "." not in host:
        raise ProvenanceError("来源必须有明确域名。")
    return [host, host[4:] if host.startswith("www.") else "www." + host]


def register(source_id, domain, tier, *, name="", kinds=(), note=""):
    """Record one reviewed source. Only version-controlled code calls this."""
    if not str(source_id or "").strip():
        raise ProvenanceError("来源必须有编号。")
    keys = _host_keys(domain)
    if not keys:
        raise ProvenanceError("来源必须有明确域名。")
    if not isinstance(tier, SourceTier):
        tier = SourceTier(str(tier))
    entry = {"source_id": source_id, "tier": tier, "name": str(name or source_id)[:120],
             "kinds": tuple(kinds), "note": str(note or "")[:400], "domain": keys[-1]}
    for key in keys:
        previous = _REGISTRY.get(key)
        if previous and previous["tier"] is not tier and previous["source_id"] != source_id:
            # Two reviewers disagreeing about one host is a configuration defect,
            # not a runtime condition: refuse rather than silently pick one.
            raise ProvenanceError(
                f"{key} 已被登记为 {previous['tier'].value}，不能同时登记为 {tier.value}。")
        _REGISTRY[key] = entry
    _REGISTRY["id:" + source_id] = entry
    return entry


def registered(host):
    return _REGISTRY.get(str(host or "").strip().lower())


def snapshot_registry():
    """The granted tiers, for a test that is about to change them.

    Restoration is preferred over clearing: wiping the registry would hide every
    reviewed source from every test that runs afterwards, which produces
    failures far from their cause.
    """
    return dict(_REGISTRY)


def restore_registry(state):
    _REGISTRY.clear()
    _REGISTRY.update(state)


def clear_registry():
    """Forget every granted tier. Only a test wanting an empty registry."""
    _REGISTRY.clear()


def tier_for_url(url):
    """The registered tier of a URL, without trusting the caller's claim."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    entry = _entry_for_host(host)
    return entry["tier"] if entry else None


def _entry_for_host(host):
    """Exact host first, then a granted subdomain, longest suffix first."""
    if not host:
        return None
    entry = _REGISTRY.get(host)
    if entry is not None:
        return entry
    parts = host.split(".")
    for index in range(1, len(parts) - 1):
        entry = _REGISTRY.get("." + ".".join(parts[index:]))
        if entry is not None:
            return entry
    return None


def _registry_entry(source):
    """Resolve a source to its registration by URL first, then by identifier.

    The URL is authoritative: a caller that labels a community post with an
    official-sounding id still resolves to whatever that *host* was granted.
    """
    url = str(source.get("url") or source.get("source_url") or "")
    if url:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
        entry = _entry_for_host(host)
        if entry is not None:
            return entry
    return _REGISTRY.get("id:" + str(source.get("id") or "").strip().lower())


def claim_from(source, field_name, value, *, tier=None, evidence_id="", fetched_at=None,
               observed_at="", corroboration=None, note="", kickoff=None, now=None, window=None):
    """Build a claim for a registered source, or refuse.

    ``tier`` is accepted only as an assertion to check against the registry, so
    a caller that tries to promote a community source is rejected here instead
    of downstream.
    """
    url = source.get("url") or source.get("source_url") or ""
    entry = _registry_entry(source)
    if entry is None:
        raise UnregisteredSource("来源未登记，不能生成可展示的主张。")
    if tier is not None:
        wanted = tier if isinstance(tier, SourceTier) else SourceTier(str(tier))
        if wanted in _TIER_ORDER and _TIER_ORDER.index(wanted) < _TIER_ORDER.index(entry["tier"]):
            raise UnregisteredSource("来源层级与登记不一致，不能提升。")
    stamp = iso(fetched_at or now) or datetime.now(timezone.utc).isoformat()
    return Claim(
        field=field_name, value=value, source_id=entry["source_id"], tier=entry["tier"],
        fetched_at=stamp, observed_at=iso(observed_at) or "",
        ttl_seconds=ttl_for(field_name, kickoff=kickoff, now=now, window=window),
        evidence_id=evidence_id, source_name=entry["name"] or source.get("name", ""),
        source_url=url, corroboration=corroboration, note=note,
    )


def reviews_required(claims, *, now=None):
    """Fields whose strongest claim has gone stale, most overdue first.

    This is what the collection scheduler asks: not "what is missing" but "what
    is no longer worth showing without a fresh look".
    """
    queue = []
    for claim in merge_claims(claims, now=now):
        ratio = claim.overdue_ratio(now)
        if ratio is None:
            # No clock at all: the source never dated it. It cannot be shown as
            # current, so it is the first thing worth going back for.
            queue.append((float("inf"), claim))
        elif ratio > 0:
            queue.append((ratio, claim))
    queue.sort(key=lambda row: row[0], reverse=True)
    return [claim for _, claim in queue]


def digest(*parts):
    """Stable identity for a claim tuple, for logs and cache keys."""
    return hashlib.sha256(json.dumps([str(part) for part in parts], ensure_ascii=False).encode()).hexdigest()[:24]
