"""The names a supporter actually types, joined to the names sources publish.

``events.ENTITY_ALIASES`` is a hand-written table covering one Premier League
squad and a handful of famous players elsewhere. It exists for a different job —
deciding which stored event a headline belongs to — and it is not a name
dictionary: a Chinese question about Manchester United's Lisandro Martínez found
no entity at all, so the search could not apply the "the article must name this
person" gate the rest of the product already relies on. The question degraded to
its filler bigrams and ranked a story about someone else.

The roster snapshots already hold the mapping that is missing: every row has the
source spelling (``name``) and, where it was verified, the mainland reading
(``name_zh``). This module turns those rows into aliases, so:

* a Chinese name finds the article that spells it in English;
* a surname alone only counts when it is unambiguous, because 马丁内斯 names both
  Lisandro Martínez and Emiliano Martínez, and treating it as one person is the
  same mistake as the namesake problem in the portrait pipeline;
* the static table is still consulted first, so nothing that worked changes.

The result is derived on demand from the store and cached by roster revision; no
name is written anywhere by this module.
"""
from __future__ import annotations

import unicodedata

from .events import ENTITY_ALIASES, contains

_cache = {"revision": None, "aliases": {}, "ambiguous": set()}


def _normalized(value):
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def surname_reading(reading):
    """The family-name part of a Chinese reading: text after the last separator.

    Readings are written 名·姓 (利桑德罗·马丁内斯) or 名姓 (哈弗茨). The last
    separator-delimited segment is what a supporter says when they are being
    brief, and it is also the part two different players most often share.
    """
    value = str(reading or "").strip()
    if not value:
        return ""
    return value.split("·")[-1].strip()


def _row_aliases(row):
    english = str(row.get("name") or "").strip()
    reading = str(row.get("name_zh") or "").strip()
    names = []
    if english:
        names.append(english)
        # A source spelling with accents is often typed without them.
        plain = "".join(char for char in unicodedata.normalize("NFKD", english) if not unicodedata.combining(char))
        if plain and plain != english:
            names.append(plain)
    family = surname_reading(reading)
    if reading:
        names.append(reading)
        if family and family != reading:
            names.append(family)
    return [name for name in dict.fromkeys(names) if len(name) >= 2]


def _build(store):
    snapshots = store.list("team_snapshots")
    aliases, owners = {}, {}
    for snapshot in snapshots:
        team_id = snapshot.get("team_id")
        for row in snapshot.get("roster") or []:
            if not isinstance(row, dict):
                continue
            provider_id = str(row.get("provider_id") or "").strip()
            if not provider_id:
                continue
            for alias in _row_aliases(row):
                key = _normalized(alias)
                aliases.setdefault(key, (provider_id, team_id, alias))
                owners.setdefault(key, set()).add(provider_id)
    # A reading shared by two players cannot name either of them. A surname is
    # exactly this case, and it is why the gate needs a single owner.
    ambiguous = {key for key, ids in owners.items() if len(ids) > 1}
    return aliases, ambiguous


def _revision(store):
    try:
        snapshots = store.list("team_snapshots")
    except Exception:
        return None
    return tuple(sorted((str(item.get("team_id")), str(item.get("updated_at") or item.get("fetched_at") or ""),
                         len(item.get("roster") or [])) for item in snapshots))


def extended_aliases(store):
    """``ENTITY_ALIASES`` plus roster readings, and the readings that are shared.

    The return value is ``(aliases, ambiguous)``: ``aliases`` maps an entity key
    to every spelling it is known by, ``ambiguous`` holds normalized readings
    that belong to more than one player and therefore cannot gate a search.
    """
    revision = _revision(store)
    if revision != _cache["revision"]:
        aliases, ambiguous = _build(store)
        registry = {key: tuple(values) for key, values in ENTITY_ALIASES.items()}
        for key, (provider_id, _team_id, alias) in aliases.items():
            entity = "player_" + provider_id
            current = list(registry.get(entity, ()))
            if alias not in current:
                current.append(alias)
            registry[entity] = tuple(current)
        _cache.update(revision=revision, aliases=registry, ambiguous=ambiguous)
    return _cache["aliases"], _cache["ambiguous"]


def named_entities(store, value):
    """The roster entities a question names, refusing shared surnames.

    Used as the mandatory-mention gate. A name that resolves to more than one
    player is dropped from the gate rather than guessing: the search then ranks
    normally instead of restricting itself to the wrong person's articles.
    """
    aliases, ambiguous = extended_aliases(store)
    found = set()
    for key, names in aliases.items():
        if not key.startswith("player_"):
            continue
        for alias in names:
            normalized = _normalized(alias)
            if normalized in ambiguous:
                continue
            if contains(_normalized(value), alias):
                found.add(key)
                break
    return found


def aliases_for(store, value):
    """Every spelling of each entity the question names, for search weighting."""
    aliases, _ambiguous = extended_aliases(store)
    result = {}
    for key in named_entities(store, value):
        result[key] = tuple(aliases.get(key, ()))
    return result
