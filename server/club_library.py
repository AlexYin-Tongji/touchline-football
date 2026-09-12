"""Shared, reviewed club catalog and bounded public research sources.

The checked-in JSON is also consumed by the browser's badge/name resolver.
Importing the catalog never downloads assets or writes user data. Competition
membership and live squad information are intentionally excluded.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import urlsplit


CATALOG_PATH = Path(__file__).resolve().parents[1] / "public/assets/clubs/catalog.json"
CLUBS = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
PROFILE_REVIEWED_AT = "2026-09-12"
BASE_FIELDS = ("id", "name", "short_name", "english_name", "color", "league")
TEAMS = [{key: club[key] for key in BASE_FIELDS} for club in CLUBS]
CLUB_PROFILES = {
    club["id"]: {key: copy.deepcopy(value) for key, value in club.items()
                 if key not in {*BASE_FIELDS, "research_sources"}}
    for club in CLUBS
}
RESEARCH_CATALOG = {club["id"]: copy.deepcopy(club["research_sources"]) for club in CLUBS}
TEAM_ALIASES = {
    club["id"]: tuple(dict.fromkeys([club["name"], club["english_name"].lower(),
                                    *(alias.lower() for alias in club["aliases"])]))
    for club in CLUBS
}
# These hosts come only from the version-controlled catalog; user-edited team
# profiles and custom source URLs must not grant trusted-network access.
TRUSTED_SOURCE_HOSTS = {
    host
    for sources in RESEARCH_CATALOG.values()
    for source in sources
    for host in (urlsplit(source["url"]).hostname,
                 *((source.get("allowed_hosts") or [])))
    if host
}
TRUSTED_SOURCE_HOSTS |= {host[4:] for host in tuple(TRUSTED_SOURCE_HOSTS) if host.startswith("www.")}


def seed_clubs(store):
    """Fill missing catalog fields; preserve custom clubs and explicit blanks."""
    for defaults in TEAMS:
        team = store.get("teams", defaults["id"])
        if team is None:
            store.put("teams", copy.deepcopy(defaults))
            continue
        missing = {key: copy.deepcopy(value) for key, value in defaults.items() if key not in team}
        if missing:
            store.put("teams", {**team, **missing})
