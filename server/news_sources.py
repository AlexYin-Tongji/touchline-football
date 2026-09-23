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
AS_HOSTS = frozenset({"feeds.as.com", "as.com"})
TYC_HOSTS = frozenset({"www.tycsports.com", "tycsports.com"})
REGIONAL_PUBLISHER_HOSTS = AS_HOSTS | TYC_HOSTS
# These shorthand names are safe in a club-specific feed, but can identify
# another club or an ordinary word worldwide. Keep stronger club identities.
AMBIGUOUS_GLOBAL_ALIASES = {
    "inter": {"inter"}, "ac-milan": {"milan", "米兰"},
    "nice": {"nice"}, "lens": {"lens"}, "angers": {"angers"},
}
EXPLICIT_GLOBAL_ALIASES = {"angers": ("sco angers",), "inter": ("inter de milán", "inter de milan", "inter de milão")}
OTHER_CLUB_IDENTITIES = {
    "arsenal": ("arsenal de sarandí", "arsenal de sarandi", "arsenal sarandi"),
    "barcelona": ("barcelona sc", "barcelona sporting", "barcelona de guayaquil"),
}


def additional_sources(team_id):
    """Only reviewed feed URLs; a global feed is filtered after index caching."""
    if team_id not in TEAM_ALIASES:
        return []
    sources = [{"id": "bbc_football", "name": "BBC 体育 · 足球", "kind": "rss",
                "url": "https://feeds.bbci.co.uk/sport/football/rss.xml",
                "allowed_hosts": sorted(BBC_HOSTS), "global_feed": True, "shared_index": True,
                "description": "BBC 足球公开订阅；按当前球队与男足一线队范围筛选。"}]
    sources.extend([
        {"id": "as_football", "name": "阿斯报 · 足球", "kind": "rss",
         "url": "https://feeds.as.com/mrss-s/pages/as/site/as.com/section/futbol/portada/",
         "allowed_hosts": sorted(AS_HOSTS), "global_feed": True, "shared_index": True,
         "reviewed_at": "2026-09-23", "description": "西班牙 AS 官方足球订阅；仅阅读公开正文，按球队筛选。"},
        {"id": "tyc_international", "name": "TyC Sports · 国际足球", "kind": "html",
         "url": "https://www.tycsports.com/futbol-internacional.html",
         "allowed_hosts": sorted(TYC_HOSTS), "global_feed": True, "shared_index": True,
         "verify_article_dates": True, "reviewed_at": "2026-09-23",
         "description": "阿根廷 TyC Sports 国际足球公开新闻；指定时间窗口时，限量读取匹配文章核实发布日期。"},
    ])
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
    if parts.hostname in AS_HOSTS:
        return not re.search(r"/(?:videos?|astv|directo|en-vivo|fotorrelato)(?:/|$)|-v/?$", path)
    if parts.hostname in TYC_HOSTS:
        return not re.search(r"/(?:videos?|vivo)(?:/|$)", path)
    return True


def global_feed_mentions_team(team_id, entry):
    # Only source-authored article metadata establishes the target club. Feed
    # labels, query wording and inferred player membership do not establish it.
    value = " ".join(str(entry.get(key) or "") for key in ("title", "description"))
    # South American clubs can share the short names of European clubs. Do not
    # assign their stories to the tracked club without a distinct identity.
    for other in OTHER_CLUB_IDENTITIES.get(team_id, ()):
        value = re.sub(re.escape(other), " ", value, flags=re.I)
    ambiguous = AMBIGUOUS_GLOBAL_ALIASES.get(team_id, ())
    aliases = (*TEAM_ALIASES.get(team_id, ()), *EXPLICIT_GLOBAL_ALIASES.get(team_id, ()))
    return any(contains(value, alias) for alias in aliases if alias not in ambiguous)
