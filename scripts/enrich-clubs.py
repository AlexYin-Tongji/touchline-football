"""Fill the club catalogue's background fields from one authority per field.

The catalogue is hand-reviewed, and 54 clubs have no founding year, 76 have no
home venue and 92 have no nickname. Adding them by hand would be 200+ edits a
reviewer cannot check; leaving them out means the team page shows a fact table
with holes in it.

So one authority is used for these three fields — Wikidata, which publishes them
as plain claims under CC0 — and the result is written back into the catalogue as
a reviewed snapshot. The application never queries Wikidata at runtime: the
facts, their source, and the date they were read all live in the version-
controlled catalogue, where a reviewer can see exactly what changed.

What is deliberately NOT taken: anything that goes stale (squad, coach,
standings), anything the catalogue already states, and any value whose subject
could not be positively identified. A club whose entity cannot be resolved is
reported, never guessed.

Usage:
    python -I scripts/enrich-clubs.py                 # plan only, prints the diff
    python -I scripts/enrich-clubs.py --write         # apply to catalog.json
    python -I scripts/enrich-clubs.py --clubs arsenal,bologna
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def use_utf8_stdout():
    """Make Chinese club names survive a Windows console.

    Done inside the entry point rather than at import: the module is imported by
    tests, and replacing `sys.stdout` there would detach the test runner's
    summary stream and close it under the runner's feet.
    """
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CATALOG = ROOT / "public/assets/clubs/catalog.json"
ATTRIBUTION = ROOT / "public/assets/clubs/attribution.json"
REVIEWED_AT = time.strftime("%Y-%m-%d")

# Which entity is a club at all. This set was originally four ids picked by
# hand and it was missing the most important one: `Q103229495`, the class for a
# plain "association football club", which is what Chelsea F.C. itself carries.
# The omission did not look like a bug — it looked like Chelsea having no
# matching entity — so the pass walked on to the next candidate and chose
# "Chelsea F.C. Development Squad and Academy". Being incomplete here is
# dangerous in a way that being strict is not: a missing class turns a correct
# entity into a visible gap, but it also lets a wrong one through.
CLUB_CLASSES = {
    "Q103229495",  # association football club
    "Q476028",     # association football club (older id, still in use)
    "Q847017",     # sports club
    "Q27197",      # football club
    "Q22905",      # association football team
    "Q245553",     # sports team
    "Q28140340",   # women's association football club — kept so the *exclusion*
                   # below can name it explicitly rather than silently skipping
}
FOOTBALL_CLASSES = CLUB_CLASSES  # kept for the acceptance check's wording

# Classes that mark an entity as *not* the senior side. Taken from P31, not from
# prose: the Chelsea academy entity describes itself as a "football club" and
# gives no other hint, but it carries Q134468886 alongside its club class.
SECOND_TEAM_CLASSES = {
    "Q134468886",  # development squad / academy
    "Q28140340",   # women's club
    "Q115606915",  # women's football club
    "Q13570292",   # reserve team
}

# Entities that pass the class check but are not the club's senior team. Held as
# a description test for the cases the classes do not cover: a plain title such
# as "Chelsea F.C. Women" says what it is even where no second-team class is set.
SECOND_TEAM_HINT = re.compile(
    r"\b(?:development|academy|reserve|reserves|youth|under-?\d{2}|u\d{2}|"
    r"women'?s?|ladies|feminine|femenino|femminile|second team|"
    r"b team|ii|subsidiary|feeder|farm)\b",
    re.I)

# Fields taken, and the Wikidata property each comes from. Every value here is
# stable for years, which is why these three and not the squad or the coach.
FIELDS = {
    "founded": "P571",         # inception
    "stadium": "P115",         # home venue
    "nickname": "P1449",       # nickname
}
VENUE_LABEL = "P115"

USER_AGENT = "TouchlineCatalog/8.5 (local single-user football reader; catalogue review)"


def get(url, *, tries=8, pause=2.0):
    """Read a Wikidata endpoint, waiting out its throttling rather than failing.

    The API answers 429/503 under light load and the limit is not documented,
    and a single club needs two or three calls (search, claims, venue label), so
    a review pass that gives up on the first refusal cannot complete 96 clubs.
    Backoff is deliberately long: this runs once, offline, and being slow costs
    nothing while being wrong costs a re-review.
    """
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            return json.load(urllib.request.urlopen(request, timeout=30))
        except urllib.error.HTTPError as exc:
            if exc.code in {429, 502, 503, 504} and attempt < tries - 1:
                time.sleep(pause * (attempt + 2))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < tries - 1:
                time.sleep(pause * (attempt + 2))
                continue
            raise
    raise RuntimeError("unreachable")


def search_terms(club):
    """The names to try, most specific first.

    The catalogue's `english_name` is the name a reader recognises, not the
    entity title: it holds "Chelsea", "Lens" and "Nice", while Wikidata titles
    the clubs "Chelsea F.C.", "RC Lens" and "OGC Nice". Searching only the short
    form fails on exactly the clubs whose names are ordinary words — a city, a
    place, a common noun — and it failed on Chelsea, Lens and Nice.

    The reviewed `aliases` are tried next. They are a better source than
    generated variants: they are already checked, they are what the club is
    actually called, and using them keeps this pass from inventing a name the
    club does not go by.
    """
    terms = [str(club.get("english_name") or "").strip()]
    terms.extend(str(alias).strip() for alias in club.get("aliases") or [])
    # Latin only: a Chinese alias cannot match an English-language search index.
    terms = [term for term in terms if term and all(ord(char) < 0x2E80 for char in term)]
    return list(dict.fromkeys(terms))[:6]


def club_identity_score(entity):
    """How confident the match is, or None to refuse the candidate.

    Ranking beats taking the first candidate that satisfies a filter, and the
    Chelsea case is why. Its first hit is the real club; the third is the
    academy. A filter that merely accepts each in turn walks past the right one
    and settles on the wrong one, silently, because both are "football clubs".
    So every candidate is scored and the best wins:

        2  a plain association football club, no second-team class
        1  another club class, no second-team class
      None a second-team class, a reserve/academy/women's description, or an
           entity that states none of the fields being collected
    """
    claims = entity.get("claims") or {}
    classes = {(claim.get("mainsnak", {}).get("datavalue", {}).get("value") or {}).get("id")
               for claim in claims.get("P31") or []}
    if not classes & CLUB_CLASSES:
        return None
    if classes & SECOND_TEAM_CLASSES:
        return None
    description = ((entity.get("descriptions") or {}).get("en") or {}).get("value") or ""
    if SECOND_TEAM_HINT.search(description):
        return None
    if not any(claims.get(prop) for prop in FIELDS.values()):
        return None
    return 2 if "Q103229495" in classes else 1


def resolve(club):
    """The Wikidata entity for the club's senior team, or None with a reason.

    Search returns namesakes freely — "Bologna" is a city, "Nice" is a place,
    "Monaco" is a country — so the class claim P31 separates the club from
    everything else that shares its name. But P31 alone is not enough, and that
    was learned the hard way: `Q2669653` is filed as an association football club
    and is titled "Chelsea F.C. Development Squad and Academy". Picking it would
    have put an academy side's founding year and ground on the first team's page.

    The candidates are scored rather than filtered, so the senior club wins over
    a reserve or women's side even when the latter appears first in the search
    results. Each search term is tried in order until one yields an acceptable
    entity; a club with none is a reported gap, never a guess.
    """
    seen = set()
    for term in search_terms(club):
        query = urllib.parse.quote(term)
        search = get("https://www.wikidata.org/w/api.php?action=wbsearchentities&format=json"
                     f"&language=en&limit=5&search={query}")
        ids = [row["id"] for row in (search.get("search") or [])
               if row.get("id") and row.get("id") not in seen]
        if not ids:
            continue
        seen.update(ids)
        entities = get("https://www.wikidata.org/w/api.php?action=wbgetentities&format=json"
                       f"&props=claims|descriptions&languages=en&ids={'|'.join(ids[:5])}")
        ranked = []
        for position, qid in enumerate(ids[:5]):
            entity = (entities.get("entities") or {}).get(qid) or {}
            score = club_identity_score(entity)
            if score is not None:
                # A stronger class wins; otherwise the API's own relevance order
                # decides, which is why the earlier position scores higher.
                ranked.append((score, -position, qid, entity))
        if ranked:
            ranked.sort(reverse=True)
            _, _, qid, entity = ranked[0]
            return qid, "resolved", entity.get("claims") or {}
        time.sleep(0.5)
    return None, "not_a_football_club", {}


def value_of(claims, prop):
    """The first statement's value, or None. Only one value is taken."""
    statements = claims.get(prop) or []
    if not statements:
        return None
    snak = statements[0].get("mainsnak", {})
    return (snak.get("datavalue") or {}).get("value")


def entry_is_complete(entry):
    """Whether a stored record would still be written as it stands.

    A stadium recorded as an entity id with no name is the shape this pass
    produced before venue names were resolved in a batched call. The write would
    drop it, so the record is not usable and has to be resolved again rather
    than inherited — otherwise a plan that looks complete yields a catalogue
    with the field silently missing.
    """
    stadium = entry.get("stadium")
    if stadium is None:
        return True
    return isinstance(stadium, dict) and bool(stadium.get("value")) and bool(stadium.get("entity"))


def entities_still_acceptable(qids):
    """Which of these stored entities would be chosen again under the rules.

    Batched, and used when resuming: the acceptance test can be tightened
    between passes, so a stored answer is re-derived rather than inherited. A
    lookup that fails re-queues its entities rather than assuming they are fine,
    because the whole point is that an unchecked answer is not evidence.
    """
    wanted = [qid for qid in dict.fromkeys(qids) if isinstance(qid, str) and qid.startswith("Q")]
    acceptable = set()
    for start in range(0, len(wanted), 50):
        chunk = wanted[start:start + 50]
        body = get("https://www.wikidata.org/w/api.php?action=wbgetentities&format=json"
                   f"&props=claims|descriptions&languages=en&ids={'|'.join(chunk)}")
        for qid, entity in (body.get("entities") or {}).items():
            if club_identity_score(entity or {}) is not None:
                acceptable.add(qid)
    return acceptable


def entity_still_acceptable(qid):
    """Single-entity form, kept for tests and ad-hoc checks."""
    if not isinstance(qid, str) or not qid.startswith("Q"):
        return False
    try:
        return qid in entities_still_acceptable([qid])
    except Exception:  # noqa: BLE001 - an unverifiable entry is re-queued
        return False


# A nickname is a display string, and Wikidata holds it in whatever language
# the statement was entered in — Aston Villa's arrives as the Greek
# "Οι Χωριάτες". A catalogue of Chinese and English club names showing one
# Greek nickname is worse than showing none, so only a value that is at least
# mostly Latin letters is taken. The rule is about the *script* the reader
# sees, not about preferring English: "Blaugrana", "Rossocrociati" and
# "Die Roten" are all kept.
LATIN_ONLY = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 '&.-–"

def latin_value(text):
    """A nickname worth showing, or None."""
    candidate = str(text or "").strip()
    if not candidate or len(candidate) > 100:
        return None
    letters = [char for char in candidate if char.isalpha()]
    if not letters:
        return None
    latin = sum(1 for char in letters if char in LATIN_ONLY)
    return candidate if latin / len(letters) >= 0.8 else None


def label_of(qid):
    body = get("https://www.wikidata.org/w/api.php?action=wbgetentities&format=json"
               f"&props=labels&languages=en&ids={qid}")
    entity = ((body.get("entities") or {}).get(qid) or {})
    return ((entity.get("labels") or {}).get("en") or {}).get("value") or ""


def labels_for(qids):
    """English labels for several entities in one request.

    Each club's home venue is a separate entity, so resolving them one at a time
    tripled the request count against an API that throttles readily. Collecting
    the venues first and asking once turns three requests per club into two,
    which is the difference between a pass that completes and one that spends
    its time waiting out 429s.
    """
    wanted = [qid for qid in dict.fromkeys(qids) if qid]
    if not wanted:
        return {}
    body = get("https://www.wikidata.org/w/api.php?action=wbgetentities&format=json"
               f"&props=labels&languages=en&ids={'|'.join(wanted[:50])}")
    out = {}
    for qid, entity in (body.get("entities") or {}).items():
        label = ((entity.get("labels") or {}).get("en") or {}).get("value")
        if label:
            out[qid] = label
    return out


def facts_for(club):
    """The three fields for one club, each with the entity it came from.

    The home venue is recorded as an entity id; its name is filled in by a later
    batched call, so this returns the id and the caller substitutes the label.
    """
    qid, status, claims = resolve(club)
    if not qid:
        return None, status, {}
    facts = {}
    inception = value_of(claims, FIELDS["founded"])
    if isinstance(inception, dict) and inception.get("time"):
        # Wikidata stores a year as "+1909-00-00T00:00:00Z" when the day is
        # unknown. Only the year is taken, and a year of 00 is not a year.
        text = str(inception["time"])
        year = text[1:5] if text[:1] in {"+", "-"} else text[:4]
        if year.isdigit() and int(year) > 1800:
            facts["founded"] = int(year)
    venue = value_of(claims, VENUE_LABEL)
    if isinstance(venue, dict) and venue.get("id"):
        # Recorded as an entity; the NAME is filled in by the caller's batched
        # label call. A venue with no English label is dropped there.
        facts["stadium"] = {"entity": venue["id"]}
    nickname = value_of(claims, FIELDS["nickname"])
    if isinstance(nickname, dict) and nickname.get("text"):
        shown = latin_value(nickname["text"])
        if shown:
            facts["nickname"] = shown
    return qid, "resolved", facts


def apply_facts(clubs, added, notes, *, reviewed_at=REVIEWED_AT):
    """Write the resolved fields into the catalogue and its attribution record.

    A field the catalogue already states is never overwritten: the catalogue is
    the reviewed record, and a pass that quietly replaced a checked founding
    year would leave no way to tell which value had been approved. Every club
    that gains a field also gains a note of where the value came from and when
    it was read, so the provenance travels with the fact.

    This is separated from the network pass so the rules it enforces — never
    overwrite, never write a blank, always record the source — can be tested
    without reaching Wikidata.
    """
    written = 0
    for club in clubs:
        gained = added.get(club["id"])
        if not gained:
            continue
        for field in ("founded", "nickname"):
            if field in gained and field not in club:
                club[field] = gained[field]
        stadium = gained.get("stadium")
        if isinstance(stadium, dict) and stadium.get("value") and "stadium" not in club:
            club["stadium"] = stadium["value"]
        club["background_sources"] = {
            **(club.get("background_sources") or {}),
            "wikidata": {"entity": gained["qid"],
                         "url": f"https://www.wikidata.org/wiki/{gained['qid']}",
                         "license": "CC0", "reviewed_at": reviewed_at,
                         "fields": sorted(field for field in gained
                                          if field != "qid" and gained[field])},
        }
        written += 1
    for row in notes.get("clubs", []):
        gained = added.get(row["id"])
        if gained:
            row["background_sources"] = {"entity": gained["qid"],
                                         "url": f"https://www.wikidata.org/wiki/{gained['qid']}",
                                         "license": "CC0", "reviewed_at": reviewed_at}
    return written


def main(args):
    clubs = json.loads(CATALOG.read_text(encoding="utf-8"))
    by_id = {club["id"]: club for club in clubs}
    selected = args.clubs.split(",") if args.clubs else [club["id"] for club in clubs]
    unknown = [name for name in selected if name not in by_id]
    if unknown:
        raise SystemExit("unknown club ids: " + ", ".join(unknown))

    # Each club is written to a plan file as soon as it is resolved, so a pass
    # that hits throttling or is interrupted can be resumed with --resume
    # instead of starting the 96-club sweep again and being throttled twice.
    plan_path = Path(args.plan) if args.plan else ROOT / "tmp/photo-exp/plan.json"
    plan = {}
    if args.resume and plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        print(f"resuming; {len(plan)} clubs already in {plan_path.name}", flush=True)

    added, skipped, failed = {}, {}, {}
    venues = {}
    requeued = []

    # A resumed run must not trust its own earlier answers. The first pass
    # resolved Chelsea to an academy side that passed the class check, and a
    # plan believed unconditionally would have carried that wrong entity into
    # the catalogue forever. The stored entities are re-checked against the
    # current rules, in batches: the check reads claims for many entities at
    # once, so re-verifying ninety clubs costs two requests rather than ninety.
    stored = {club_id: entry.get("qid") for club_id, entry in plan.items() if club_id in by_id}
    if stored:
        try:
            acceptable = entities_still_acceptable(list(stored.values()))
        except Exception as exc:  # noqa: BLE001 - an unverifiable plan is re-queued
            print(f"could not re-check stored entities ({type(exc).__name__}); re-queueing all",
                  flush=True)
            acceptable = set()
        for club_id, qid in stored.items():
            entry = plan.get(club_id) or {}
            # Two things are re-checked, not one. The entity has to still pass
            # the identity rules, AND the record has to be complete: an entry
            # written before venue names were resolved carries a stadium with
            # an entity id and no name, and is a record the write would have to
            # drop. Trusting it because its entity is fine left eight English
            # clubs with no ground at all.
            if qid not in acceptable or not entry_is_complete(entry):
                plan.pop(club_id)
                requeued.append(club_id)
    if requeued:
        print(f"re-queued {len(requeued)} clubs whose stored entity no longer passes: "
              + ", ".join(sorted(requeued)), flush=True)

    def name_venues(pending):
        """One batched call names every home venue, instead of one per club.

        It runs periodically rather than only at the end, because this pass is
        interrupted by throttling: a run that stopped early would otherwise
        leave every club it did resolve without its venue name.
        """
        if not pending:
            return
        try:
            names = labels_for(list(pending.values()))
        except Exception as exc:  # noqa: BLE001 - a review pass reports
            print(f"venue names unavailable ({type(exc).__name__}); stadium fields left out", flush=True)
            return
        for club_id, entity in pending.items():
            name = latin_value(names.get(entity))
            gained = added[club_id]
            if name:
                gained["stadium"] = {"value": name, "entity": entity}
            else:
                # No English label, or one in another script. The field is
                # omitted rather than stored blank: an empty stadium renders as
                # an empty fact row, which reads worse than not knowing.
                gained.pop("stadium", None)
            plan[club_id] = gained
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"venue names resolved for {sum(1 for c in pending if added[c].get('stadium'))}/{len(pending)} clubs",
              flush=True)

    for index, club_id in enumerate(selected, 1):
        club = by_id[club_id]
        if club_id in plan:
            added[club_id] = plan[club_id]
            continue
        missing = [field for field in ("founded", "stadium", "nickname") if not club.get(field)]
        if not missing:
            skipped[club_id] = "complete"
            continue
        try:
            qid, status, facts = facts_for(club)
        except Exception as exc:  # noqa: BLE001 - a review pass reports
            failed[club_id] = f"{type(exc).__name__}: {exc}"
            print(f"[{index:>3}] {club_id:<24} ERROR {failed[club_id][:60]}", flush=True)
            time.sleep(1.0)
            continue
        if not qid:
            failed[club_id] = status
            print(f"[{index:>3}] {club_id:<24} {status}", flush=True)
            time.sleep(1.0)
            continue
        gained = {field: value for field, value in facts.items() if field in missing}
        if isinstance(gained.get("stadium"), dict) and gained["stadium"].get("entity"):
            venues[club_id] = gained["stadium"]["entity"]
        added[club_id] = {"qid": qid, **gained}
        plan[club_id] = added[club_id]
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        shown = " ".join(f"{k}={v if not isinstance(v, dict) else v['entity']}"
                         for k, v in gained.items()) or "(nothing new)"
        print(f"[{index:>3}] {club_id:<24} {qid:<12} {shown}", flush=True)
        time.sleep(1.0)
        # Name the venues collected so far every few clubs. Waiting until the
        # end would lose them whenever throttling ends the pass early, and this
        # pass is routinely interrupted.
        if len(venues) >= 8:
            name_venues(venues)
            venues.clear()

    name_venues(venues)

    print(f"\nresolved with new fields: {len(added)}  already complete: {len(skipped)}  "
          f"unresolved: {len(failed)}")
    if failed:
        print("unresolved clubs:")
        for club_id, reason in sorted(failed.items()):
            print(f"   {club_id:<24} {reason}")
    if not args.write:
        print(f"\nplan only; {len(added)} clubs ready, rerun with --write to apply")
        return
    if not added:
        print("nothing to write")
        return

    # The catalogue is the reviewed record, so the new values go in beside a
    # note of where they came from and when they were read.
    notes = json.loads(ATTRIBUTION.read_text(encoding="utf-8"))
    written = apply_facts(clubs, added, notes)
    CATALOG.write_text(json.dumps(clubs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ATTRIBUTION.write_text(json.dumps(notes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {written} clubs to {CATALOG.relative_to(ROOT)}")


if __name__ == "__main__":
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--clubs")
    parser.add_argument("--plan")
    parser.add_argument("--resume", action="store_true")
    main(parser.parse_args())
