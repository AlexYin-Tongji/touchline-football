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
RMC_HOSTS = frozenset({"rmcsport.bfmtv.com"})
MARCA_HOSTS = frozenset({"e00-marca.uecdn.es", "www.marca.com", "marca.com"})
GAZZETTA_HOSTS = frozenset({"www.gazzetta.it", "gazzetta.it"})
KICKER_HOSTS = frozenset({"newsfeed.kicker.de", "www.kicker.de", "kicker.de"})
ESPN_HOSTS = frozenset({"www.espn.com", "espn.com", "www.espn.co.uk", "espn.co.uk"})
TMW_HOSTS = frozenset({"www.tuttomercatoweb.com", "tuttomercatoweb.com"})
FOOTMERCATO_HOSTS = frozenset({"www.footmercato.net", "footmercato.net"})
REGIONAL_PUBLISHER_HOSTS = AS_HOSTS | TYC_HOSTS | RMC_HOSTS | MARCA_HOSTS | GAZZETTA_HOSTS | KICKER_HOSTS | TMW_HOSTS | FOOTMERCATO_HOSTS

# Hosts that serve more than one reviewed subscription. A transport fault there
# says nothing about one feed path, so the failure is recorded against the host
# and shared by every feed on it instead of being re-observed per subscription.
SHARED_FEED_HOSTS = frozenset({"feeds.bbci.co.uk"})


def shared_host(url):
    """Whether one host-level health record covers this feed's siblings."""
    try:
        return (urlsplit(url).hostname or "").lower() in SHARED_FEED_HOSTS
    except ValueError:
        return False
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
    """Reviewed shared publisher feeds, with dense national football channels.

    Channels remain separate operational sources; publisher_id prevents two
    subscriptions from pretending to be independent editorial voices.
    """
    if team_id not in TEAM_ALIASES:
        return []
    sources = [{"id": "bbc_football", "name": "BBC 体育 · 足球", "kind": "rss",
                "url": "https://feeds.bbci.co.uk/sport/football/rss.xml",
                "allowed_hosts": sorted(BBC_HOSTS), "global_feed": True, "shared_index": True,
                "reviewed_at": "2026-09-24",
                "description": "BBC 足球公开订阅；按当前球队与男足一线队范围筛选。"}]
    sources.extend([
        {"id": "sky_football", "name": "天空体育 · 足球", "kind": "rss",
         "url": "https://www.skysports.com/rss/11095", "allowed_hosts": sorted(SKY_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-09-23",
         "description": "天空体育官方足球订阅；排除直播、视频及其他球队消息后阅读公开文章。"},
        {"id": "rmc_football", "name": "RMC Sport · 足球", "kind": "rss",
         "url": "https://rmcsport.bfmtv.com/rss/football/", "allowed_hosts": sorted(RMC_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-09-23",
         "description": "RMC Sport 官方足球订阅；仅收录公开文章，排除直播、视频和人物目录。"},
        {"id": "as_football", "name": "阿斯报 · 足球", "kind": "rss",
         "url": "https://feeds.as.com/mrss-s/pages/as/site/as.com/section/futbol/portada/",
         "allowed_hosts": sorted(AS_HOSTS), "global_feed": True, "shared_index": True,
         "reviewed_at": "2026-09-23", "description": "西班牙 AS 官方足球订阅；仅阅读公开正文，按球队筛选。"},
        {"id": "tyc_international", "name": "TyC Sports · 国际足球", "kind": "html",
         "url": "https://www.tycsports.com/futbol-internacional.html",
         "allowed_hosts": sorted(TYC_HOSTS), "global_feed": True, "shared_index": True,
         "verify_article_dates": True, "reviewed_at": "2026-09-23",
         "description": "阿根廷 TyC Sports 国际足球公开新闻；指定时间窗口时，限量读取匹配文章核实发布日期。"},
        {"id": "espn_soccer", "name": "ESPN · 足球", "kind": "rss",
         "url": "https://www.espn.com/espn/rss/soccer/news", "allowed_hosts": sorted(ESPN_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-09-24",
         "description": "ESPN 足球公开订阅；按球队筛选后阅读公开正文。"},
        {"id": "marca_football", "name": "马卡报 · 足球", "kind": "rss",
         "url": "https://e00-marca.uecdn.es/rss/futbol.xml", "allowed_hosts": sorted(MARCA_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-09-24",
         "description": "西班牙马卡报官方足球订阅；仅阅读公开正文，按球队筛选。"},
        {"id": "gazzetta_football", "name": "米兰体育报 · 足球", "kind": "rss",
         "url": "https://www.gazzetta.it/rss/calcio.xml", "allowed_hosts": sorted(GAZZETTA_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-09-24",
         "description": "意大利米兰体育报足球订阅；仅阅读公开正文，按球队筛选。"},
        {"id": "kicker_football", "name": "踢球者 · 足球", "kind": "rss",
         "url": "https://newsfeed.kicker.de/news/aktuell", "allowed_hosts": sorted(KICKER_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-09-24",
         "description": "德国踢球者公开订阅；仅阅读公开正文，按球队筛选。"},
        {"id": "tmw_football", "name": "TuttoMercatoWeb · 足球", "kind": "rss",
         "url": "https://www.tuttomercatoweb.com/rss/", "allowed_hosts": sorted(TMW_HOSTS),
         "global_feed": True, "shared_index": True, "publisher_id": "tmw", "reviewed_at": "2026-10-01",
         "declaration_url": "https://www.tuttomercatoweb.com/rss-lista/",
         "description": "意大利足球媒体主站明确发布的综合订阅；按球队与一线队范围筛选，正文须另行阅读。"},
        {"id": "tmw_serie_a", "name": "TuttoMercatoWeb · 意甲", "kind": "rss",
         "url": "https://www.tuttomercatoweb.com/rss/?s=1", "allowed_hosts": sorted(TMW_HOSTS),
         "global_feed": True, "shared_index": True, "publisher_id": "tmw", "reviewed_at": "2026-10-01",
         "declaration_url": "https://www.tuttomercatoweb.com/rss-lista/",
         "description": "主站订阅目录明确发布的意甲频道，补充本国联赛报道密度；仍按相同球队身份与文章范围筛选。"},
        {"id": "footmercato_football", "name": "Foot Mercato · 足球", "kind": "rss",
         "url": "https://www.footmercato.net/flux-rss", "allowed_hosts": sorted(FOOTMERCATO_HOSTS),
         "global_feed": True, "shared_index": True, "publisher_id": "footmercato", "reviewed_at": "2026-10-01",
         "declaration_url": "https://www.footmercato.net/contact-mentions-legales-flux-rss",
         "description": "法国足球媒体官方说明页发布的综合订阅；保留媒体来源归属，不将转会报道写成已确认球队事实。"},
    ])
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
    if re.search(r"/(?:media[-_]videos?|videos?|watch|live(?:-blog)?)(?:/|$)", path):
        return False
    if parts.hostname in BBC_HOSTS:
        return not re.search(r"/(?:sounds|av|videos?|live)(?:/|$)", path)
    if parts.hostname in SKY_HOSTS:
        return not re.search(r"/(?:watch|videos?|live(?:-blog)?)(?:/|$)", path)
    if parts.hostname in AS_HOSTS:
        return not re.search(r"/(?:videos?|astv|directo|en-vivo|fotorrelato)(?:/|$)|-v/?$", path)
    if parts.hostname in TYC_HOSTS:
        return not re.search(r"/(?:videos?|vivo)(?:/|$)", path)
    if parts.hostname in RMC_HOSTS:
        return bool(re.search(r"_av-\d+\.html$", path))
    if parts.hostname in ESPN_HOSTS:
        return bool(re.search(r"/story/_/id/\d+/", path))
    if parts.hostname in MARCA_HOSTS:
        return not re.search(r"/(?:videos?|directo|en-vivo|minuto-a-minuto)(?:/|$)", path)
    if parts.hostname in GAZZETTA_HOSTS:
        return not re.search(r"/(?:diretta|video|videos)(?:/|$)", path)
    if parts.hostname in KICKER_HOSTS:
        return not re.search(r"/(?:live|video|videos)(?:/|$)", path)
    return True


def global_feed_mentions_team(team_id, entry):
    # Only source-authored article metadata establishes the target club. Feed
    # labels, query wording and inferred player membership do not establish it.
    host = urlsplit(entry.get("url", "")).hostname
    # RMC summaries often cite a former club only as biographical background.
    # Headlines identify the subject more reliably; OM is its established
    # Marseille shorthand in this French football feed.
    value = (str(entry.get("title") or "") if host in RMC_HOSTS else
             " ".join(str(entry.get(key) or "") for key in ("title", "description")))
    if host in RMC_HOSTS and team_id == "marseille" and contains(value, "om"):
        return True
    # South American clubs can share the short names of European clubs. Do not
    # assign their stories to the tracked club without a distinct identity.
    for other in OTHER_CLUB_IDENTITIES.get(team_id, ()):
        value = re.sub(re.escape(other), " ", value, flags=re.I)
    ambiguous = AMBIGUOUS_GLOBAL_ALIASES.get(team_id, ())
    aliases = (*TEAM_ALIASES.get(team_id, ()), *EXPLICIT_GLOBAL_ALIASES.get(team_id, ()))
    return any(contains(value, alias) for alias in aliases if alias not in ambiguous)
