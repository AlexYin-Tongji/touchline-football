"""Reviewed publisher feeds and their narrow metadata compatibility rules."""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from . import ingestion
from .club_library import TEAM_ALIASES
from .events import contains


BBC_HOSTS = frozenset({"feeds.bbci.co.uk", "www.bbc.co.uk", "www.bbc.com", "bbc.co.uk", "bbc.com"})
SKY_HOSTS = frozenset({"www.skysports.com", "skysports.com"})
FEED_HOSTS = frozenset({"feeds.bbci.co.uk", "www.skysports.com"})
# These shorthand names are safe in a club-specific feed, but can identify
# another club or an ordinary word worldwide. Keep stronger club identities.
AMBIGUOUS_GLOBAL_ALIASES = {
    "inter": {"inter"}, "ac-milan": {"milan", "米兰"},
    "nice": {"nice"}, "lens": {"lens"}, "angers": {"angers"},
}
EXPLICIT_GLOBAL_ALIASES = {"angers": ("sco angers",)}


def additional_sources(team_id):
    """Only reviewed feed URLs; a global feed is filtered after index caching."""
    if team_id not in TEAM_ALIASES:
        return []
    sources = [{"id": "bbc_football", "name": "BBC 体育 · 足球", "kind": "rss",
                "url": "https://feeds.bbci.co.uk/sport/football/rss.xml",
                "allowed_hosts": sorted(BBC_HOSTS), "global_feed": True, "shared_index": True,
                "description": "BBC 足球公开订阅；按当前球队与男足一线队范围筛选。"}]
    if team_id == "arsenal":
        sources.append({"id": "sky_arsenal", "name": "天空体育 · 阿森纳", "kind": "rss",
                        "url": "https://www.skysports.com/rss/11670", "allowed_hosts": sorted(SKY_HOSTS),
                        "description": "天空体育已核验的阿森纳公开订阅源。"})
    return sources


def feed_published_time(value, url):
    # Sky's feed uses UK abbreviations that email date parsing does not always
    # recognise. Never infer a missing timezone or apply BST to another host.
    if isinstance(value, str) and urlsplit(url).hostname in SKY_HOSTS:
        value = re.sub(r"\s+(BST|GMT)\s*$", lambda match: " +0100" if match[1] == "BST" else " +0000", value)
    return ingestion.published_time(value)


def readable_publisher_entry(entry):
    parts = urlsplit(entry.get("url", ""))
    path = unquote(parts.path).lower()
    if parts.hostname in BBC_HOSTS:
        return not re.search(r"/(?:sounds|av|videos?|live)(?:/|$)", path)
    if parts.hostname in SKY_HOSTS:
        return not re.search(r"/(?:watch|videos?|live(?:-blog)?)(?:/|$)", path)
    return True


def global_feed_mentions_team(team_id, entry):
    # Only source-authored article metadata establishes the target club. Feed
    # labels, query wording and inferred player membership do not establish it.
    value = " ".join(str(entry.get(key) or "") for key in ("title", "description"))
    ambiguous = AMBIGUOUS_GLOBAL_ALIASES.get(team_id, ())
    aliases = (*TEAM_ALIASES.get(team_id, ()), *EXPLICIT_GLOBAL_ALIASES.get(team_id, ()))
    return any(contains(value, alias) for alias in aliases if alias not in ambiguous)
