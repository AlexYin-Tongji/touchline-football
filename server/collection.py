"""What the agent should go and check next, and why.

Today every information source is refreshed on its own fixed timer or when a
person presses a button. That does not scale to a hundred clubs, and it does not
answer the question that actually matters: *what is currently least trustworthy
to show?* A birthday and a match result both being "a bit old" are not the same
problem, and a fixture thirty hours away changes which squad facts are urgent.

This module turns that question into a queue. It reads the claims already
stored, asks each one when it stopped being worth showing, and orders them by
how far past their own budget they are. Emphasis on *their own*: a month-old
result is far more overdue than a year-old birthday, even though the birthday is
older in seconds. `provenance.Claim.overdue_ratio` is the measure, and it is the
whole reason the review order is not simply "oldest first".

Two things this deliberately does not do yet. It does not fetch anything — it
decides, and something else acts. And it does not invent a priority beyond
overdueness; user interest and editorial judgement belong here eventually, but
adding a weight nobody has calibrated would only make the queue harder to reason
about.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import provenance
from .provenance import Claim, SourceTier

# What each capability costs, so a plan can be made against a budget rather than
# against hope. A metered provider is the binding constraint on how much can be
# learned per hour, so the plan is built in that order.
COST_WEIGHT = {"free": 0, "light": 1, "quota": 4, "key": 6}


@dataclass
class Need:
    """One thing worth re-checking, and the reason it ranks where it does."""

    subject_id: str
    field: str
    overdue_ratio: float
    reason: str
    capability: str = ""
    tier: str = ""
    last_checked: str = ""
    blocking: bool = False

    def safe(self):
        return {"subject_id": self.subject_id, "field": self.field,
                "overdue_ratio": round(self.overdue_ratio, 3), "reason": self.reason,
                "capability": self.capability, "tier": self.tier,
                "last_checked": self.last_checked, "blocking": self.blocking}


@dataclass
class Plan:
    """A bounded answer to "what should be refreshed now"."""

    needs: list = field(default_factory=list)
    budget: int = 0
    spent: int = 0
    skipped: list = field(default_factory=list)

    @property
    def empty(self):
        return not self.needs

    def safe(self):
        return {"needs": [need.safe() for need in self.needs], "budget": self.budget,
                "spent": self.spent, "skipped": self.skipped}


def _claim_rows(subject):
    """Every claim attached to one subject, whatever shape it was stored in.

    Stored claims come back as plain dictionaries, so the tier has to be turned
    back into its enum here. A row that cannot be rebuilt is skipped rather than
    raising: one malformed record must not stop the whole queue being planned.
    """
    rows = []

    def rebuild(value):
        if isinstance(value, Claim):
            return value
        if not isinstance(value, dict) or "field" not in value or "source_id" not in value:
            return None
        try:
            return Claim(**{key: value[key] for key in Claim.__dataclass_fields__ if key in value})
        except provenance.ProvenanceError:
            return None

    for value in (subject or {}).values():
        row = rebuild(value)
        if row is not None:
            rows.append(row)
    claims = (subject or {}).get("claims")
    if isinstance(claims, dict):
        for value in claims.values():
            if isinstance(value, list):
                rows.extend(row for row in (rebuild(item) for item in value) if row is not None)
                continue
            row = rebuild(value)
            if row is not None:
                rows.append(row)
    return rows


def _capability_for(field_name):
    """Which provider capability can actually answer this field."""
    name = str(field_name or "")
    if name in {"photo_url"}:
        return "image"
    if name in {"name", "birth_date", "nationality", "height_cm", "weight_kg", "position",
                "position_group", "club_id", "bio"}:
        return "person"
    if name in {"roster", "squad", "number", "availability", "injury", "lineup"}:
        return "squad"
    if name in {"score", "result", "kickoff", "fixture", "schedule"}:
        return "fixture"
    if name in {"standings", "league_id", "season_id"}:
        return "standings"
    return "news"


def needs_from(subjects, *, now=None, limit=40):
    """Rank what is worth re-checking, most overdue first.

    ``subjects`` is any mapping of subject id to something carrying claims (a
    player, a match, a team). Claims with no clock at all rank first: material
    the source never dated cannot be shown as current.
    """
    moment = now or datetime.now(timezone.utc)
    needs = []
    for subject_id, subject in (subjects or {}).items():
        for claim in _claim_rows(subject):
            ratio = claim.overdue_ratio(moment)
            if ratio is None:
                needs.append(Need(str(subject_id), claim.field, float("inf"),
                                  "来源没有提供时间，无法作为当前资料展示。",
                                  _capability_for(claim.field), claim.tier.value, ""))
                continue
            if ratio <= 0:
                continue
            needs.append(Need(str(subject_id), claim.field, ratio,
                              _reason(ratio, claim),
                              _capability_for(claim.field), claim.tier.value,
                              claim.fetched_at or ""))
    needs.sort(key=lambda need: (-(need.overdue_ratio or 0), need.subject_id, need.field))
    return needs[:max(1, int(limit))] if limit else needs


def _reason(ratio, claim):
    if ratio >= 10:
        return f"{claim.field} 已超过其时效预算 {ratio:.0f} 倍，展示前应重新确认。"
    if ratio >= 1:
        return f"{claim.field} 已过时效预算，建议重新确认。"
    return f"{claim.field} 接近时效上限。"


def plan(needs, *, budget=12):
    """Take as many needs as the budget allows, most urgent first.

    The queue is ordered here rather than trusted to the caller: a plan built
    from an unsorted list would silently spend the budget on whatever happened
    to come first, which is the failure mode this module exists to prevent.

    A need that does not fit is skipped rather than ending the plan, so one
    expensive item cannot starve the cheaper and more urgent ones behind it.
    """
    remaining = max(0, int(budget))
    chosen, skipped, spent = [], [], 0
    for need in sorted(needs, key=lambda item: (-(item.overdue_ratio or 0), item.subject_id, item.field)):
        weight = COST_WEIGHT.get(_cost_for(need), 1)
        if weight > remaining:
            skipped.append({**need.safe(), "skipped_because": "本轮预算不足"})
            continue
        chosen.append(need)
        remaining -= weight
        spent += weight
    return Plan(needs=chosen, budget=budget, spent=spent, skipped=skipped[:20])


def _cost_for(need):
    """The cheapest registered provider that can answer this need.

    A need answerable by an unmetered source is free even when a metered source
    could also answer it; choosing the metered one would spend quota to learn
    something already available.
    """
    from . import providers
    best = None
    for row in providers.sources(capability=need.capability or "news"):
        weight = COST_WEIGHT.get(row.cost.value, 1)
        if best is None or weight < best:
            best = weight
    return _cost_name(best if best is not None else 1)


def _cost_name(weight):
    for name, value in COST_WEIGHT.items():
        if value == weight:
            return name
    return "light"


def next_plan(subjects, *, budget=12, limit=40, now=None):
    """The whole decision in one call: read what is stored, say what to check."""
    return plan(needs_from(subjects, now=now, limit=limit), budget=budget)


def summary(plan_):
    """A short line for the interface, since a plan is not meant to be read raw."""
    if plan_.empty:
        return "当前资料都在各自时效内，无需更新。"
    top = plan_.needs[0]
    if top.overdue_ratio == float("inf"):
        # An undated value has no ratio to quote; saying "infinite times its
        # budget" is not a sentence a reader can use.
        return f"本轮计划更新 {len(plan_.needs)} 项，最紧要的是 {top.subject_id} 的 {top.field}（来源未提供时间）。"
    return (f"本轮计划更新 {len(plan_.needs)} 项，最紧要的是 {top.subject_id} 的 {top.field}"
            f"（超出时效 {top.overdue_ratio:.0f} 倍）。")
