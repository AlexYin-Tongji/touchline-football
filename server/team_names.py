"""Where a player's Chinese name comes from, and what makes it citable.

Two routes, one identity check.

The identity check is the part that matters. A name is not a translation and
not a guess — "萨卡" is not derivable from "Saka" — so a reading is only ever
*read off* a source, and the source has to be about the right person. What makes
that check strong is that all of it already exists: ``wikimedia_portraits``
resolves a player to exactly one English Wikipedia article by comparing the full
name, the birth date, the occupation and the club against the roster row, and
records the Wikidata entity behind it. This module reuses those comparisons
rather than inventing a second, weaker standard — the roster row is the same, the
birth date it compares is the same, and a whole-year birth-date mismatch still
means the reading is refused.

What was missing was the *reading*. The portrait pass only reached the Wikidata
label as a side effect of a photo lookup, and only for the players whose photo
happened to resolve — which is why a squad can be 27 strong with 6 Chinese
names. It also had a second problem the measurements made visible: Wikidata's
``zh`` label mixes Mainland, Hong Kong and Taiwan conventions, so a real
Bundesliga squad yielded 马田·达尼亚 for Martin Terrier where the reading a
Mainland reader expects is 马丁·泰里耶.

So the order here is Chinese Wikipedia first — its article titles follow the
Mainland naming convention — and the Wikidata label as the fallback for the
players who have no article there. Both are read off a source either way, and
the source each one came from travels with the reading, because they disagree
often enough that it has to.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import re

from .ingestion import IngestionError, now
from .player_names import NAME_SOURCES, cjk_label, to_simplified
from .wikimedia_portraits import (HOSTS, WIKIPEDIA, WIKIDATA, _birth_date, _candidate_name,
                                  _current_club, _name, _safe_url, request_json)

WIKIPEDIA_ZH = "zh.wikipedia.org"
# The Chinese Wikipedia API is the same pinned endpoint shape as the English
# one, but it is a different host and has to be reviewed as one. Kept beside the
# module that uses it rather than added to the portrait allowlist, which guards
# a different kind of read.
ALLOWED_HOSTS = frozenset({WIKIPEDIA, WIKIPEDIA_ZH, WIKIDATA})
MAX_SEARCH_RESULTS = 5
# A reading is a checked fact about a person, not a perishable fetch. The short
# window is for verdicts; a name that was right stays right until the roster row
# it was checked against changes, which the proof itself detects.
NAME_SECONDS = 90 * 86400
WIKIDATA_SOURCE = "Wikidata 中文标签"
WIKIPEDIA_SOURCE = "维基百科中文条目"


def _cache_key(player, team_id):
    """Bound to the three facts the reading was checked against.

    Not the portrait key: that one is built from the name and birth date alone,
    while a name proof is also tied to the ``provider_id`` the roster row
    carried. Rebuilding the roster can change that id without changing the
    person, and the proof has to be re-earned when it does rather than silently
    carried over.
    """
    material = f"{team_id}|{_name(player.get('name'))}|{player.get('birth_date') or ''}|{player.get('provider_id') or ''}"
    return "wikidata_name_v1_" + sha256(material.encode()).hexdigest()[:24]


async def _zh_title(store, title):
    """The Chinese article title for an English one, if it has a Chinese link."""
    response = await _load(store, WIKIPEDIA, {"action": "query", "format": "json", "prop": "langlinks",
                                        "lllang": "zh", "titles": title, "redirects": 1})
    for page in ((response or {}).get("query", {}).get("pages", {}) or {}).values():
        links = page.get("langlinks")
        if isinstance(links, list) and links and isinstance(links[0], dict):
            value = links[0].get("*")
            if cjk_label(value):
                return value
    return ""


async def _wikidata_zh(store, qid):
    """The published Chinese label on an entity we already trust."""
    response = await _load(store, WIKIDATA, {"action": "wbgetentities", "format": "json", "ids": qid,
                                      "props": "labels", "languages": "zh-hans|zh"})
    entity = ((response or {}).get("entities") or {}).get(qid) or {}
    if (not isinstance(entity, dict) or entity.get("id", qid) != qid
            or any(flag in entity for flag in ("missing", "invalid", "suppressed"))
            or any(entity.get(flag) is True for flag in ("private", "deleted", "restricted"))):
        return None
    labels = entity.get("labels") if isinstance(entity.get("labels"), dict) else {}
    for language in ("zh-hans", "zh"):
        label = labels.get(language) or {}
        if not isinstance(label, dict) or label.get("language") != language:
            continue
        published = label.get("value")
        if not cjk_label(published):
            continue
        simplified = to_simplified(published)
        if not cjk_label(simplified):
            continue
        return {"language": language, "published": published, "reading": simplified}
    return None


async def _load(store, host, params):
    """One pinned API read against the hosts this module is allowed to touch.

    ``wikimedia_portraits.request_json`` is reused rather than reimplemented: it
    owns the host allowlist, the 2.2-second global pace, the 429 backoff and the
    in-flight de-duplication. A second copy of that logic would be a second set
    of rate limits to keep in step.
    """
    from .wikimedia_portraits import request_json as _request
    if host not in ALLOWED_HOSTS:
        raise IngestionError("球员中文名来源地址未获允许。")
    return await _request(store, host, params)


async def _article(store, title):
    """The article's short description and opening text, for the identity checks."""
    response = await _load(store, WIKIPEDIA, {"action": "query", "format": "json", "prop": "extracts|pageprops",
                                       "exintro": 1, "explaintext": 1, "titles": title, "redirects": 1})
    for page in ((response or {}).get("query", {}).get("pages", {}) or {}).values():
        if page.get("missing") is not None:
            continue
        return {"title": page.get("title") or title,
                "short": (page.get("pageprops") or {}).get("wikibase-shortdesc") or "",
                "extract": page.get("extract") or ""}
    return None


async def _entity_for_title(store, title):
    """The Wikidata entity a Wikipedia title sits on, via its page props."""
    response = await _load(store, WIKIPEDIA, {"action": "query", "format": "json", "prop": "pageprops",
                                       "titles": title, "redirects": 1})
    for page in ((response or {}).get("query", {}).get("pages", {}) or {}).values():
        qid = (page.get("pageprops") or {}).get("wikibase_item")
        if isinstance(qid, str) and re.fullmatch(r"Q[1-9][0-9]{0,11}", qid):
            return qid
    return None


async def resolve_reading(store, player, team_id):
    """The reading a roster row can be given, with the proof that earned it.

    Returns ``(record, reason)``: a record ready to be stored and handed to
    ``checked_wikidata_reading``, or ``None`` plus why not. Every refusal path
    says which check failed, because "no Chinese name" and "the source has no
    such person" are different answers and the caller has to be able to tell
    them apart.
    """
    name, birth = player.get("name"), player.get("birth_date")
    if not _name(name):
        return None, "missing_name"
    if not birth:
        # Without a birth date there is no way to tell one 张伟 from another, and
        # a name attached to the wrong person is worse than an English one.
        return None, "missing_birth_date"

    found = await _load(store, WIKIPEDIA, {"action": "query", "format": "json", "list": "search",
                                     "srsearch": f'"{name}" footballer', "srlimit": MAX_SEARCH_RESULTS})
    titles = [item.get("title") for item in ((found or {}).get("query", {}).get("search") or [])
              if isinstance(item, dict) and isinstance(item.get("title"), str)]
    # The full-name comparison is what makes a hit a candidate at all.
    exact = [title for title in titles if _candidate_name(title, birth) == _name(name)]
    if not exact:
        return None, "no_matching_article"
    if len(exact) > 1:
        return None, "ambiguous_identity"

    title = exact[0]
    article = await _article(store, title)
    if not article:
        return None, "unreadable_article"
    description = f"{article['short']} {article['extract'][:500]}".lower()
    if "footballer" not in description and "football player" not in description:
        return None, "not_a_footballer"
    published_birth = _birth_date(article["extract"][:300])
    if published_birth != birth:
        # The whole-year mismatch that blocks the 53 known namesakes. A reading
        # from the wrong person's article is exactly what must not be published.
        return None, "birth_date_mismatch"
    club = _current_club(article["extract"], team_id)
    if club == "other_club":
        return None, "other_club"

    qid = await _entity_for_title(store, article["title"])
    if not qid:
        return None, "no_entity"

    reading, source = None, None
    zh_title = await _zh_title(store, article["title"])
    if zh_title:
        # Chinese Wikipedia titles follow the Mainland convention, so this route
        # is preferred when the player has an article there.
        simplified = to_simplified(zh_title)
        if cjk_label(simplified) and cjk_label(zh_title):
            reading, source = simplified, {"name": WIKIPEDIA_SOURCE, "published": zh_title,
                                           "language": "zh"}
    if reading is None:
        label = await _wikidata_zh(store, qid)
        if label:
            reading, source = label["reading"], {"name": WIKIDATA_SOURCE, "published": label["published"],
                                                 "language": label["language"]}
    if reading is None:
        return None, "no_chinese_reading"

    proof = {"qid": qid, "label": source["published"], "published_label": source["published"],
             "language": source["language"], "method": "wikidata_existing_identity_check",
             "name": name, "team_id": team_id, "roster_birth_date": birth,
             "provider_id": player.get("provider_id"), "article_title": article["title"],
             "article_url": f"https://en.wikipedia.org/wiki/{article['title'].replace(' ', '_')}",
             # Only carried when the reading came from a Chinese article: the
             # verifier rebuilds the citation from the published title, so the
             # recorded title and the cited URL cannot drift apart.
             **({"zh_title": source["published"]} if source["name"] == WIKIPEDIA_SOURCE else {}),
             "unique_article": True, "identity_checked": True, "public": True}
    # The citation is built by the same function the verifier checks it against,
    # from the same proof. Two constructions of one URL is exactly how a stored
    # proof ends up being rejected by the check it was written for.
    record = {"name_status": "verified", "name_zh": reading, "name_zh_source": source["name"],
              "name_zh_source_url": NAME_SOURCES[source["name"]]["url"](proof),
              "name_zh_checked_at": now(), "name_zh_identity": proof, "checked_at": now()}
    return record, None


def cached_reading(store, player, team_id):
    """A stored reading for this row, re-checked before it is handed back.

    Storage is not proof. The record is passed through the same verifier the
    portrait path uses, so a reading whose ``provider_id`` no longer matches the
    row — which is what a roster rebuild does — is dropped here rather than
    published as if it still applied.
    """
    from .player_names import checked_wikidata_reading
    record = store.get_meta(_cache_key(player, team_id))
    if not isinstance(record, dict):
        return None
    checked_at = record.get("checked_at")
    try:
        stamp = datetime.fromisoformat(str(checked_at or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if stamp.tzinfo is None or (datetime.now(timezone.utc) - stamp).total_seconds() >= NAME_SECONDS:
        return None
    return checked_wikidata_reading(record, player, team_id=team_id)


def store_reading(store, player, team_id, record):
    store.set_meta(_cache_key(player, team_id), record)
    return record
