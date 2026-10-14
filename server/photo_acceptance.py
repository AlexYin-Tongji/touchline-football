"""Judge what a photo pass actually achieved, and decide what is worth another look.

The loop this closes was missing an acceptance step, not a repeat step. A team
cycle could already be re-run, and a cursor already rotated which twelve players
were asked about — but nothing ever judged the outcome, so "portraits are not
fresh" and "portraits are impossible" were the same state to the caller, and the
next pass had no way to know whether the last one helped.

The judgement here is code, deliberately. A model may arrange *who to ask about
next* (see ``photo_planning``), but whether a squad's coverage improved, and
whether the remaining gaps are reachable at all, are facts about the data that
can be computed and asserted. Letting a model decide whether work is finished
would put a paid, non-deterministic answer behind a boolean that a test can
otherwise pin down.

The split between the three verdicts is the whole point. A gap caused by a
throttle is worth retrying; a gap caused by a roster with no birth date is
recoverable only by fixing the roster; a gap where the source holds a different
person is not recoverable at all and retrying it forever is just noise.

This module reads a row's stored ``reason_code`` and nothing else. It never
touches a photo address, a licence or an identity claim, so it cannot widen what
a caller is allowed to decide.
"""
from __future__ import annotations

# Reasons that describe the *route*, not the player: the request never got an
# answer. All of these are worth another attempt.
TRANSPORT_REASONS = frozenset({
    "timeout", "dns_failed", "connection_error", "connection_reset",
    "tls_error", "tls_certificate", "unavailable", "http_rate_limited",
    "http_unavailable", "not_yet_resolved", "source_unavailable",
    # Not a transport fault, but the same instruction: the saved photo could not
    # be re-derived under the current rules, so the source has to be asked again.
    "identity_revalidation",
})

# Reasons that describe *this roster entry* being unmatchable because the roster
# itself lacks a fact the sources need. Fixable — but by correcting the roster,
# not by asking the photo sources again.
ROSTER_REASONS = frozenset({
    "missing_name", "missing_nationality",
})

# Reasons where the sources were reached, answered, and do not hold this player
# at all. No amount of retrying or roster-fixing changes this: the record is
# absent from the database, or the only records under that name are other
# people. Recorded apart from the roster gaps so the reader is not told to fix
# something that is not broken.
SOURCE_GAP_REASONS = frozenset({
    "community_missing", "community_no_image", "missing_article", "no_free_image",
    # The id-anchored Wikidata lookup was asked and holds no such ESPN id.
    # Retrying the same statement does not create the entity.
    "wikidata_no_espn_id",
})

# Reasons where the source answered with a *different person who shares the
# name*. This is a gap in what the source will return, not a refusal of the
# player and not a fault in the request, so retrying the same query is
# pointless — but it is worth reporting separately from "no record", because a
# different source or a higher authorisation tier can still reach them. Kept
# out of ``retryable`` (nothing about a retry would change the answer) and out
# of ``source_gaps`` (the source does hold the player; it will not return them
# beside the namesake it already chose).
NAMESAKE_CODE = "community_namesake_blocked"
NAMESAKE_REASONS = frozenset({NAMESAKE_CODE})

# Reasons where the source holds more than one candidate and cannot say which is
# this player. A retry could resolve it if the source's data changes, so it is
# not settled, but a roster fact would also help — it is kept apart so neither
# "just retry" nor "fix the roster" is claimed on its own.
AMBIGUOUS_REASONS = frozenset({
    "community_ambiguous", "ambiguous_identity",
})

# Reasons that are a durable answer about the player, usually because the source
# holds a different person or refuses the image. A pass that keeps asking is
# burning paced requests to be told the same thing. The two bare codes are the
# fallbacks a source sets when it has a verdict but no specific code, so they are
# treated as settled rather than as an unknown fault to retry.
SETTLED_REASONS = frozenset({
    "license_not_free", "source_withdrawn",
    "rejected", "community_rejected",
    # The ESPN id points at an entity whose stated birth date is a different
    # person. Asking the same statement again will not change that.
    "wikidata_birth_conflict",
})

# How a gap should be approached on the next pass.
FIX_ROUTE = "fix_route"          # the transport failed; ask again
FIX_ROSTER = "fix_roster"        # the roster lacks the facts a source needs
FIX_SOURCE = "fix_source"        # the sources hold no such player
FIX_NAMESAKE = "fix_namesake"    # a same-named stranger occupies the only slot
FIX_AMBIGUOUS = "fix_ambiguous"  # more than one candidate, none separable
FIX_NOTHING = "fix_nothing"      # the sources have answered; stop asking
FIX_UNKNOWN = "fix_unknown"      # never attempted

# The kinds of gap, in the order they are reported. One tuple so the breakdown
# dictionary, the message and the tests cannot drift apart.
GAP_KINDS = (FIX_ROUTE, FIX_ROSTER, FIX_SOURCE, FIX_NAMESAKE,
             FIX_AMBIGUOUS, FIX_NOTHING, FIX_UNKNOWN)

# Coverage below this share, with everything else settled, is reported as a
# shortfall rather than as an irreducible gap.
ACCEPTABLE_COVERAGE = 0.9


def classify(reason_code):
    """Which kind of gap a stored reason code describes."""
    code = str(reason_code or "").strip()
    if not code:
        return FIX_UNKNOWN
    if code in TRANSPORT_REASONS:
        return FIX_ROUTE
    if code in ROSTER_REASONS:
        return FIX_ROSTER
    if code in SOURCE_GAP_REASONS:
        return FIX_SOURCE
    if code in NAMESAKE_REASONS:
        return FIX_NAMESAKE
    if code in AMBIGUOUS_REASONS:
        return FIX_AMBIGUOUS
    if code in SETTLED_REASONS:
        return FIX_NOTHING
    # A code this module does not know is treated as retryable: an unknown
    # failure is more likely a new transport error than a durable verdict, and
    # treating it as settled would silently stop looking.
    return FIX_ROUTE


def _row_reason(row):
    # A row the source answered with a same-named stranger carries the fact as
    # its own flag rather than as a reason code, because the code is still the
    # source's own verdict about this roster entry ("ambiguous", "missing") and
    # overwriting it would lose what the source actually said. The flag is what
    # the *approach* has to follow, so it is read first here.
    if row.get("namesake_blocked"):
        return NAMESAKE_CODE
    return row.get("reason_code") or ("not_yet_resolved" if row.get("retryable") else "")


def assess(roster):
    """What one squad's photos stand at, and what the gaps are made of.

    ``roster`` is the stored snapshot's roster list. Returns counts and a
    per-kind breakdown; ``accepted`` is the code's own answer to "is there
    anything worth doing next", which is what a loop reads to decide whether to
    run another pass. The verdict never depends on a model.
    """
    rows = [row for row in (roster or []) if isinstance(row, dict)]
    total = len(rows)
    with_photo = [row for row in rows if row.get("photo_url")]
    missing = [row for row in rows if not row.get("photo_url")]
    breakdown = {kind: [] for kind in GAP_KINDS}
    for row in missing:
        breakdown[classify(_row_reason(row))].append({
            "player_id": str(row.get("provider_id") or row.get("id") or ""),
            "name": row.get("name") or "",
            "reason_code": _row_reason(row) or "not_yet_resolved",
        })
    coverage = (len(with_photo) / total) if total else 1.0
    # There is work to do only when a gap is attributable to a route we can try
    # again. A roster fact that could be supplied is real work too, but it is
    # the roster tool's job, not another photo pass over the same names.
    actionable = len(breakdown[FIX_ROUTE])
    return {
        "total": total,
        "with_photo": len(with_photo),
        "missing": len(missing),
        "coverage": round(coverage, 4),
        "accepted": actionable == 0,
        "actionable_count": actionable,
        "retryable": breakdown[FIX_ROUTE],
        "needs_roster_facts": breakdown[FIX_ROSTER],
        "source_gaps": breakdown[FIX_SOURCE],
        "namesake_blocked": breakdown[FIX_NAMESAKE],
        "ambiguous": breakdown[FIX_AMBIGUOUS],
        "settled": breakdown[FIX_NOTHING],
        "unattempted": breakdown[FIX_UNKNOWN],
        "counts": {kind: len(items) for kind, items in breakdown.items()},
    }


def shortfall(report, *, threshold=ACCEPTABLE_COVERAGE):
    """What a planner may be asked to order when a pass should run again.

    Route failures first, then the ambiguous entries — the only two kinds where
    another looked-up answer could differ. Gaps the sources have already
    answered, and gaps that belong to the roster, are left out: asking again for
    a player the database does not hold is the behaviour that made the old loop
    look busy while making no progress.
    """
    if report.get("coverage", 1.0) >= threshold:
        return []
    return list(report.get("retryable") or []) + list(report.get("ambiguous") or [])


def summarize(report, previous=None):
    """One line for the reader, and whether this pass moved anything.

    ``previous`` is an earlier ``assess`` result for the same squad. Comparing
    the two is the acceptance test: a pass that gained nothing and still has
    route failures is worth retrying, while one whose gaps are all answers from
    the sources is finished, and saying so is more useful than reporting "not
    fresh" a fourth time.
    """
    counts = report.get("counts") or {}
    parts = []
    if counts.get(FIX_ROUTE):
        parts.append(f"{counts[FIX_ROUTE]} 名球员的照片来源本轮未读取成功")
    if counts.get(FIX_AMBIGUOUS):
        parts.append(f"{counts[FIX_AMBIGUOUS]} 名球员在来源中存在多个同名候选")
    if counts.get(FIX_NAMESAKE):
        parts.append(f"{counts[FIX_NAMESAKE]} 名球员在现有来源中被同名球员挡住，需换来源")
    if counts.get(FIX_SOURCE):
        parts.append(f"{counts[FIX_SOURCE]} 名球员在现有来源中没有记录")
    if counts.get(FIX_ROSTER):
        parts.append(f"{counts[FIX_ROSTER]} 名球员缺少来源核对所需的阵容资料")
    if counts.get(FIX_NOTHING):
        parts.append(f"{counts[FIX_NOTHING]} 名球员的来源已明确拒绝")
    if not parts:
        return "全部球员照片已通过核验。" if report.get("missing") == 0 else "照片核对已完成本轮。"
    return "；".join(parts) + "。"


def should_continue(report, previous=None, *, attempts=0, max_attempts=3):
    """Whether another pass is worth starting, as a code decision.

    Three separate stops, each for a different reason:

    * nothing actionable is left, so every remaining gap is an answer rather
      than a failure
    * the previous pass gained nothing, so the route or the source — not the
      batch order — is what stands in the way
    * the attempt budget is spent, so an unattended loop cannot run forever
    """
    if not report.get("actionable_count"):
        return False
    if attempts >= max_attempts:
        return False
    if isinstance(previous, dict) and "with_photo" in previous:
        if (report.get("with_photo", 0) <= previous["with_photo"]
                and report.get("actionable_count", 0) >= previous.get("actionable_count", 0)):
            # No gain, and as much still outstanding as before: another pass
            # over the same players would repeat the same failures.
            return False
    return True
