"""The record domain of the shared review loop: names, player facts, clubs, crests.

Photos have pictures to look at. This domain has none — it reviews the *residue*
of the resolvers that read structured records: a Chinese name two publishers
spell differently, a squad row the source shipped without a birth date, a club
search term that matched nothing, a badge with no attribution behind it.

The split is the same one ``review_loop`` exists to draw. Code answers "is this
source record about this person" and does not bend; what it cannot answer is
which of several equally-good readings is the better bet, whether a miss was a
throttle or a wrong term, and which of thirty incomplete rows is worth a lookup
this pass. Those are judgement calls, and they are the only thing a model is
asked for here.

Nothing in this module writes a value. A decision is handed back to the caller,
which runs it through the same source-backed collector it always uses; the model
never supplies a name, a year, a nationality, or a search term of its own. That
is also why a crest never becomes a picture to look at: a badge's bytes are
hashed into its attribution record and its rights belong to the club, so the
open question about a badge is coverage — is there a source and an attribution —
which code answers exactly.
"""
from __future__ import annotations

from . import review_loop

ACTION_KEEP = review_loop.ACTION_KEEP
ACTION_RETRY = review_loop.ACTION_RETRY
ACTION_INVESTIGATE = review_loop.ACTION_INVESTIGATE

# A row the resolver refused for a reason worth another attempt. Codes the
# resolver treats as durable answers are absent on purpose: re-asking a source
# that already said "no such player" is the busywork the acceptance module was
# built to stop.
RETRYABLE_NAME_REASONS = frozenset({
    "timeout", "dns_failed", "connection_error", "connection_reset", "http_rate_limited",
    "ambiguous_identity", "missing_article", "no_candidate", "missing_name",
})
# Roster facts a source may simply not have shipped. The community source
# refuses a row with neither nationality nor birth date before it makes a
# request, so these decide whether a photo lookup can even be attempted — which
# is a different question from whether the photo source has a record.
IDENTITY_FACTS = ("nationality", "birth_date")


def name_items(roster):
    """Players whose Chinese name is unresolved in a way a retry could change."""
    items = []
    for player in roster or []:
        if not isinstance(player, dict) or player.get("name_zh"):
            continue
        code = str(player.get("name_zh_reason_code") or player.get("reason_code") or "")
        if code and code not in RETRYABLE_NAME_REASONS:
            continue
        items.append({"item_id": "name:" + _key(player),
                      "domain": "name", "name": player.get("name"),
                      "state": "name_unresolved" if code else "name_untried",
                      "reason_code": code or "not_yet_resolved",
                      "detail": player.get("nationality"), "attempts": player.get("name_attempts")})
    return items


def player_fact_items(roster):
    """Rows the roster feed shipped without the facts a lookup needs.

    This is the gap the photo pipeline cannot see on its own. A row with neither
    nationality nor birth date makes the community source refuse before any
    request is sent, so it sits in every photo pass reporting a transport-shaped
    gap for a reason that is not transport at all. Code can say *which* facts are
    absent; whether that is worth a targeted lookup, and which player to spend it
    on, is the judgement this item exists to ask for.
    """
    items = []
    for player in roster or []:
        if not isinstance(player, dict):
            continue
        missing = [field for field in IDENTITY_FACTS if not player.get(field)]
        if not missing:
            continue
        items.append({"item_id": "facts:" + _key(player), "domain": "player_facts",
                      "name": player.get("name"),
                      "state": "no_identity_facts" if len(missing) == len(IDENTITY_FACTS)
                               else "missing_" + "_and_".join(missing),
                      "reason_code": "source_omitted_facts",
                      "detail": "、".join(missing), "attempts": None})
    return items


def club_items(team):
    """The club record's own unresolved fields, read from the team row.

    ``club_profile`` writes collected fields straight onto the team record, so
    the record itself is where a gap is visible — there is no separate stored
    report to consult, and reading one would have been reading the wrong object.
    """
    from . import club_profile
    team = team if isinstance(team, dict) else {}
    items = []
    missing = club_profile.missing_fields(team)
    if missing:
        items.append({"item_id": "club:" + str(team.get("id") or ""), "domain": "club",
                      "name": team.get("name") or team.get("english_name"),
                      "state": "club_fields_missing", "reason_code": "wikidata_field_absent",
                      "detail": "、".join(club_profile.FIELD_LABELS[field] for field in missing),
                      "attempts": None})
    return items


def crest_items(team):
    """A badge whose coverage is incomplete — never a picture to judge.

    The three states want different next steps and only code can tell them
    apart: a badge with no address or no attribution is a rights question, one
    pointing off-site is a shipping question, and a complete local asset is
    finished. What is left to judge is whether a missing attribution can be
    recovered from the record we already hold, or whether it needs looking up.
    """
    from . import club_profile
    state = club_profile.crest_state(team)
    if state.get("status") == "fresh":
        return []
    missing = state.get("missing") or []
    return [{"item_id": "crest:" + str((team or {}).get("id") or ""), "domain": "crest",
             "name": (team or {}).get("name") or (team or {}).get("english_name"),
             "state": "crest_" + str(state.get("status") or "unknown"),
             "reason_code": "remote_asset" if state.get("status") == "remote" else "incomplete_record",
             "detail": "、".join(str(field) for field in missing) or None, "attempts": None}]


def _key(player):
    return str(player.get("provider_id") or player.get("id") or "")


def record_items(roster, team=None):
    """Every record-domain item for one club, in a stable order."""
    return [*name_items(roster), *player_fact_items(roster),
            *club_items(team), *crest_items(team)]


async def review_records(store, items, *, requester=None, timeout=60):
    """One bounded round over the record items. Returns the decisions."""
    if not items:
        return []
    return await review_loop.review(store, items, requester=requester, timeout=timeout,
                                    session="record_review")
