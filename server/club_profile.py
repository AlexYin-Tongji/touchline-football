"""Club identity fields a reader sees before a ball is kicked.

Everything here comes from one place with one licence: Wikidata (CC0) answers
the founding year, the home venue and the nickname. That is a deliberate
restriction, not a limitation of effort. The field a reader is most likely to
notice is wrong — which club this is — is exactly the field where a second
source with different editorial conventions turns a checked value into an
argued one. One source, one licence, one reviewed value, and the provenance
travels with the fact.

The pass only ever *fills*. A value the record already states is left alone:
the catalogue on disk and any edit made in the interface are both reviewed
records, and a pass that quietly replaced one would leave no way to tell which
value had been approved. Blank is also not written — "we did not find it" and
"there is nothing there" are different statements about a club.

This mirrors ``scripts/enrich-clubs.py``, which filled the shipped catalogue
offline. The rules are the same because the failure they prevent is the same:
the Chelsea entity search returns the real club first and the academy third,
and a filter that merely accepts the first candidate satisfying "is a football
club" walks past the right answer and silently settles on the wrong one.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .ingestion import IngestionError, now

WIKIDATA = "www.wikidata.org"
ENTITY_URL = "https://www.wikidata.org/wiki/"

# The properties read, and what each one is called in the record.
FOUNDED_PROPERTY = "P571"    # inception
VENUE_PROPERTY = "P115"      # home venue
NICKNAME_PROPERTY = "P1449"  # nickname
INSTANCE_PROPERTY = "P31"    # instance of
# Wikidata classes that mean "this is a football club at all". A candidate whose
# P31 shares none of these is refused outright rather than merely ranked lower:
# "Bologna" is a city, "Nice" is a place, "Monaco" is a country, and a scoring
# scheme that accepted them as weak candidates would eventually publish one.
ASSOCIATION_FOOTBALL_CLUB = "Q103229495"   # association football club (current)
FOOTBALL_CLUB_CLASSES = frozenset({
    ASSOCIATION_FOOTBALL_CLUB,
    "Q476028",       # association football club (older item, still in wide use)
    "Q245065",       # football club
})
# Classes that mean this entity is *not* the first team, whatever else it claims.
SECOND_TEAM_CLASSES = frozenset({
    "Q19674581",     # association football club reserve team
    "Q13393265",     # reserve team
    "Q2374196",      # youth team
    "Q1156686",      # women's association football team
    "Q1350310",      # farm team
})
SECOND_TEAM_WORDS = re.compile(
    r"\b(?:academy|reserves?|reserve team|youth|under-?\d{2}|u\d{2}|women'?s|"
    r"development squad|b team|ii)\b", re.I)

# Values the interface reads as a football club. Anything else is not shown, so
# it is not collected either.
FIELDS = ("founded", "stadium", "nickname")
FIELD_LABELS = {"founded": "创立年份", "stadium": "主场", "nickname": "昵称"}
# A founding year does not go off. There is no "check again next week" for a
# value that has been stable since 1886, and treating it as perishable would
# re-read Wikidata on every visit for a field that cannot have changed.
RECHECK_SECONDS = 365 * 24 * 3600


def _name(value):
    return " ".join(str(value or "").split()).strip()


def _latin(value):
    """A nickname only in Latin script.

    Wikidata keeps a declaration in the language it was declared in, and Aston
    Villa's is "Οι Χωριάτες". Dropped rather than transliterated: a romanisation
    this pass invented would sit in the record looking like a published name.

    The test is "is every letter in the Latin ranges", not "is every character
    below the CJK block". The looser test passed Greek, Cyrillic and Hebrew
    through, because all of those sit below U+2E80 — which is exactly the case
    the shipped script was written to catch, so it would have put Aston Villa's
    Greek nickname into a catalogue that had deliberately left it empty.
    """
    text = _name(value)
    if not text:
        return ""
    letters = [char for char in text if char.isalpha()]
    if not letters or not all("a" <= char.casefold() <= "z" for char in letters):
        return ""
    # No digits-only or punctuation-only strings, and no control characters.
    return text if all(char.isprintable() for char in text) else ""


def claim_values(claims, prop):
    return [row for row in (claims or {}).get(prop, []) if isinstance(row, dict)]


def _datavalue(claim):
    value = ((claim or {}).get("mainsnak") or {}).get("datavalue") or {}
    return value.get("value") if isinstance(value, dict) else None


def entity_ids(value):
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)]


def identity_score(entity):
    """How confident the match is, or None to refuse the candidate.

    Ranking beats accepting the first candidate that passes a filter, and the
    Chelsea case is why: its search results hold the club and its academy, both
    of them "association football club" entities. Scoring every candidate and
    taking the best is what keeps the academy from being written in place of the
    club, silently, with a plausible-looking founding year.

    Three rules, and each one refuses a mistake that actually happened while
    this was being written against live Wikidata:

      2  a plain association football club that states a collected field
      1  another club class that states a collected field
    None  not a club class at all — "Bologna" is a city, "Nice" is a place,
          "Monaco" is a country, and all three share their name with a club and
          are returned by the same search
    None  a second-team, reserve, academy, youth or women's class
    None  describes itself as one of those, whatever its class claims
    None  claims no founding year, venue or nickname — with nothing to collect,
          accepting it can only put the wrong entity's name into the record
    """
    if not isinstance(entity, dict):
        return None
    classes = set()
    for claim in claim_values(entity.get("claims"), INSTANCE_PROPERTY):
        classes.update(entity_ids(_datavalue(claim)))
    if not classes & FOOTBALL_CLUB_CLASSES:
        return None
    if classes & SECOND_TEAM_CLASSES:
        return None
    label = _name((entity.get("labels") or {}).get("en", {}).get("value"))
    description = _name((entity.get("descriptions") or {}).get("en", {}).get("value"))
    if SECOND_TEAM_WORDS.search(label) or SECOND_TEAM_WORDS.search(description):
        return None
    claims = entity.get("claims") or {}
    if not any(claims.get(prop) for prop in (FOUNDED_PROPERTY, VENUE_PROPERTY, NICKNAME_PROPERTY)):
        return None
    return 2 if ASSOCIATION_FOOTBALL_CLUB in classes else 1


def facts_from(entity, *, labels=None):
    """The three fields, from one entity and its referenced venue label."""
    facts = {}
    claims = (entity or {}).get("claims") or {}
    inception = _datavalue(next(iter(claim_values(claims, FOUNDED_PROPERTY)), None))
    time_value = inception.get("time") if isinstance(inception, dict) else None
    if isinstance(time_value, str):
        # Wikidata stores "1909-00-00T00:00:00Z" when the day is unknown. Only
        # the year is taken, and a year of 00 is not a year.
        text = time_value[1:5] if time_value[:1] in {"+", "-"} else time_value[:4]
        if text.isdigit() and 1800 < int(text) <= datetime.now(timezone.utc).year:
            facts["founded"] = int(text)
    venue_ids = []
    for claim in claim_values(claims, VENUE_PROPERTY):
        venue_ids.extend(entity_ids(_datavalue(claim)))
    if venue_ids and labels:
        venue = _name(labels.get(venue_ids[0]))
        if venue:
            facts["stadium"] = venue
    for claim in claim_values(claims, NICKNAME_PROPERTY):
        value = _datavalue(claim)
        text = value.get("text") if isinstance(value, dict) else None
        shown = _latin(text)
        if shown:
            facts["nickname"] = shown
            break
    return facts


def missing_fields(team):
    """Which of the three the record does not state yet."""
    missing = []
    for field in FIELDS:
        current = (team or {}).get(field)
        if current in (None, "", 0):
            missing.append(field)
    return tuple(missing)


def apply_facts(team, facts, *, qid, entity_url=None, reviewed_at=None):
    """Merge collected fields into the record. Returns the fields written.

    Never overwrites and never writes a blank, so the pass is safe to re-run:
    a second run over a filled record finds nothing to do and changes nothing.
    The provenance is recorded on the same write as the value, so a fact can
    never end up in the record without the entity it came from.
    """
    written = []
    for field in FIELDS:
        value = (facts or {}).get(field)
        if value in (None, "", 0):
            continue
        if (team or {}).get(field) not in (None, "", 0):
            continue
        team[field] = value
        written.append(field)
    if not written:
        return []
    team["background_sources"] = {
        **((team or {}).get("background_sources") or {}),
        "wikidata": {"entity": qid,
                     "url": entity_url or ENTITY_URL + qid,
                     "license": "CC0",
                     "reviewed_at": reviewed_at or now()[:10],
                     "fields": sorted(written)},
    }
    return written


async def _get(store, params):
    """One pinned Wikidata read, through the same reviewed transport as photos."""
    from .wikimedia_portraits import request_json
    return await request_json(store, WIKIDATA, params)


async def search_entities(store, term, *, limit=5):
    """Candidate entities for one name, best-first by the search index."""
    payload = await _get(store, {"action": "wbsearchentities", "format": "json",
                                "search": term, "language": "en", "uselang": "en",
                                "type": "item", "limit": limit})
    return [row.get("id") for row in payload.get("search") or []
            if isinstance(row, dict) and re.fullmatch(r"Q[1-9]\d{0,11}", str(row.get("id") or ""))]


async def entity(store, qid, *, props="claims|labels|descriptions", languages="en"):
    payload = await _get(store, {"action": "wbgetentities", "format": "json", "ids": qid,
                                "props": props, "languages": languages})
    entity = (payload.get("entities") or {}).get(qid)
    if (not isinstance(entity, dict) or entity.get("id", qid) != qid
            or any(flag in entity for flag in ("missing", "invalid", "suppressed"))
            or any(entity.get(flag) is True for flag in ("private", "deleted", "restricted"))):
        return None
    return entity


async def labels_for(store, qids):
    """English labels for a batch of entity ids."""
    wanted = [qid for qid in dict.fromkeys(qids) if re.fullmatch(r"Q[1-9]\d{0,11}", str(qid))]
    if not wanted:
        return {}
    payload = await _get(store, {"action": "wbgetentities", "format": "json",
                                "ids": "|".join(wanted), "props": "labels", "languages": "en"})
    result = {}
    for qid, row in (payload.get("entities") or {}).items():
        label = ((row or {}).get("labels") or {}).get("en", {}).get("value")
        if _name(label):
            result[qid] = _name(label)
    return result


async def resolve(store, team):
    """The best entity for one club, or None with the reason it was refused.

    Search terms come from the record's own reviewed names — the English name
    and the checked aliases — most specific first. A generated variant is never
    used: the aliases are the names the club is actually called, and searching a
    name nobody uses is how a pass finds a plausible wrong entity.
    """
    terms = [_name(team.get("english_name"))]
    terms.extend(_name(alias) for alias in team.get("aliases") or [])
    # A Chinese alias cannot match an English-language index.
    terms = [term for term in terms if term and all(ord(char) < 0x2E80 for char in term)]
    terms = list(dict.fromkeys(terms))[:6]
    if not terms:
        return None, "missing_name"
    seen = {}
    for term in terms:
        for position, qid in enumerate(await search_entities(store, term)):
            if qid in seen:
                continue
            candidate = await entity(store, qid)
            if candidate is None:
                continue
            score = identity_score(candidate)
            if score is None:
                continue
            # Ranked by class first, then by the search index's own relevance
            # order — the earlier a candidate appears, the more likely it is the
            # entity the term actually names. Ties do not fall back to dict
            # order, which is insertion order and has nothing to do with either.
            seen[qid] = (score, -position, candidate)
    if not seen:
        return None, "no_candidate"
    qid, (_, _, best) = max(seen.items(), key=lambda row: (row[1][0], row[1][1]))
    return best, qid


async def collect(store, team):
    """Collect this club's missing identity fields, without writing them.

    Returns ``{"team", "summary"}``. The caller persists the returned record, so
    the write happens wherever the other maintenance writes do and a reader can
    always see one place that changes a club.

    Nothing is looked up when the record already states every field, which is
    the common case and costs no network request at all.
    """
    missing = missing_fields(team)
    if not missing:
        return {"team": team, "written": [],
                "summary": {"status": "completed", "missing": [], "written": [],
                            "note": "球队资料已完整，无需补充。"}}
    found, qid = await resolve(store, team)
    if found is None:
        return {"team": team, "written": [],
                "summary": {"status": "missing", "missing": list(missing), "written": [],
                            "reason_code": qid,
                            "note": "维基数据没有找到可确认的球队条目，资料保持原样。"}}
    venue_ids = []
    for claim in claim_values(found.get("claims"), VENUE_PROPERTY):
        venue_ids.extend(entity_ids(_datavalue(claim)))
    labels = await labels_for(store, venue_ids) if venue_ids else {}
    facts = facts_from(found, labels=labels)
    resolved_qid = str(found.get("id") or qid)
    written = apply_facts(team, facts, qid=resolved_qid)
    return {"team": team, "written": written, "qid": resolved_qid,
            "summary": {"status": "completed", "entity": resolved_qid,
                        "entity_url": ENTITY_URL + resolved_qid, "license": "CC0",
                        "missing": list(missing), "written": written, "found": sorted(facts),
                        "note": ("已按维基数据补充：" + "、".join(written)) if written
                                else "维基数据未给出可补充的字段，资料保持原样。"}}


def crest_state(team):
    """What can be said about this club's crest without fetching it.

    The crest is a released asset, not something a pass downloads: it ships with
    the application, its bytes are hashed into the attribution record, and the
    rights belong to the club. So the honest report is coverage — is a local
    asset referenced, and does it carry the source and attribution the interface
    needs — not a model's opinion of whether the picture looks right.

    Deliberately never returns or fetches image bytes. This is the whole reason
    a club badge is not part of the photo pipeline: that pipeline decides which
    *person* a face belongs to, and there is no equivalent question to ask about
    a badge.
    """
    url = str((team or {}).get("crest_url") or "")
    source = str((team or {}).get("crest_source_url") or "")
    attribution = str((team or {}).get("crest_attribution") or "")
    local = url.startswith("/assets/")
    missing = [name for name, value in (("crest_url", url), ("crest_source_url", source),
                                        ("crest_attribution", attribution)) if not value]
    if missing:
        status = "missing"
        note = "队徽缺少" + "、".join(missing) + "，界面按缺图留空处理。"
    elif not local:
        status = "remote"
        note = "队徽指向站外地址；本地资产不需要浏览器跨站取图。"
    else:
        status = "fresh"
        note = "队徽来自本地资产并带来源与署名。"
    return {"status": status, "url": url, "local_asset": local,
            "source_url": source, "attribution": attribution,
            "missing": missing, "note": note}
