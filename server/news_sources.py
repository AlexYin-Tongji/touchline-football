"""Reviewed publisher feeds and their narrow metadata compatibility rules."""
from __future__ import annotations

import json
import re
from pathlib import Path
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
BUT_FOOTBALL_HOSTS = frozenset({"www.butfootballclub.fr", "butfootballclub.fr"})
REGIONAL_PUBLISHER_HOSTS = AS_HOSTS | TYC_HOSTS | RMC_HOSTS | MARCA_HOSTS | GAZZETTA_HOSTS | KICKER_HOSTS | TMW_HOSTS | FOOTMERCATO_HOSTS

# French-language football desks. Two rules key off this set: a club whose name
# is also an ordinary English word is unambiguous here, and "OM" is the
# established French shorthand for Marseille rather than a word to be ignored.
FRENCH_PUBLISHER_HOSTS = RMC_HOSTS | FOOTMERCATO_HOSTS | BUT_FOOTBALL_HOSTS

# English-language football desks verified by direct read on 2026-10-08: each
# returned a parsable feed and article text with no proxy. They widen the
# non-domestic coverage an English club gets, which was otherwise ESPN alone.
FOOTBALL_ITALIA_HOSTS = frozenset({"www.football-italia.net", "football-italia.net"})
NINETY_MIN_HOSTS = frozenset({"www.90min.com", "90min.com"})
METRO_HOSTS = frozenset({"metro.co.uk", "www.metro.co.uk"})
MIRROR_HOSTS = frozenset({"www.mirror.co.uk", "mirror.co.uk"})
CAUGHT_OFFSIDE_HOSTS = frozenset({"www.caughtoffside.com", "caughtoffside.com"})
HARD_TACKLE_HOSTS = frozenset({"www.thehardtackle.com", "thehardtackle.com"})
FOOTBALL_LONDON_HOSTS = frozenset({"www.football.london", "football.london"})
MUNDO_DEPORTIVO_HOSTS = frozenset({"www.mundodeportivo.com", "mundodeportivo.com"})
TUTTOSPORT_HOSTS = frozenset({"www.tuttosport.com", "tuttosport.com"})
RECORD_HOSTS = frozenset({"www.record.pt", "record.pt"})
GIVEMESPORT_HOSTS = frozenset({"www.givemesport.com", "givemesport.com"})
THE42_HOSTS = frozenset({"www.the42.ie", "the42.ie"})
HITC_HOSTS = frozenset({"www.hitc.com", "hitc.com"})

# Non-English desks, verified by direct read on 2026-10-08. The global feeds
# were overwhelmingly English or one-nation: a Spanish club got Marca and AS, a
# French club Foot Mercato, and everyone else English-language coverage of
# other people's leagues. These carry the domestic report a club's own
# supporters actually read, in the language its press writes in.
CALCIOMERCATO_HOSTS = frozenset({"feeds.footballco.com"})
DI_MARZIO_HOSTS = frozenset({"www.gianlucadimarzio.com", "gianlucadimarzio.com"})
MILANNEWS_HOSTS = frozenset({"www.milannews.it", "milannews.it"})
RELEVO_HOSTS = frozenset({"www.relevo.com", "relevo.com"})
DIARIO_GOL_HOSTS = frozenset({"www.diariogol.com", "diariogol.com"})
ESTADIO_DEPORTIVO_HOSTS = frozenset({"www.estadiodeportivo.com", "estadiodeportivo.com"})
MAISFUTEBOL_HOSTS = frozenset({"maisfutebol.iol.pt"})
VI_NL_HOSTS = frozenset({"www.vi.nl", "vi.nl"})
NON_ENGLISH_PUBLISHER_HOSTS = (CALCIOMERCATO_HOSTS | DI_MARZIO_HOSTS | MILANNEWS_HOSTS | RELEVO_HOSTS
                               | DIARIO_GOL_HOSTS | ESTADIO_DEPORTIVO_HOSTS | BUT_FOOTBALL_HOSTS
                               | MAISFUTEBOL_HOSTS | VI_NL_HOSTS)
ANGLO_PUBLISHER_HOSTS = (FOOTBALL_ITALIA_HOSTS | NINETY_MIN_HOSTS | METRO_HOSTS
                         | MIRROR_HOSTS | CAUGHT_OFFSIDE_HOSTS | HARD_TACKLE_HOSTS | FOOTBALL_LONDON_HOSTS
                         | MUNDO_DEPORTIVO_HOSTS | TUTTOSPORT_HOSTS | RECORD_HOSTS
                         | GIVEMESPORT_HOSTS | THE42_HOSTS | HITC_HOSTS
                         | NON_ENGLISH_PUBLISHER_HOSTS)

# Supporter publications that publish a feed. They carry a byline and a date
# where a forum thread carries neither, and they are relayed as community
# claims: nothing found here is written into a structured field, and a feed
# carries no agreement signal so it never sets corroboration.
COMMUNITY_FEEDS = (
    {"id": "arseblog", "name": "Arseblog", "url": "https://arseblog.news/feed/",
     "hosts": frozenset({"arseblog.news"}), "teams": ("arsenal",)},
    {"id": "barca_blaugranes", "name": "Barca Blaugranes", "url": "https://www.barcablaugranes.com/rss/index.xml",
     "hosts": frozenset({"www.barcablaugranes.com", "barcablaugranes.com"}), "teams": ("barcelona",)},
    {"id": "managing_madrid", "name": "Managing Madrid", "url": "https://www.managingmadrid.com/rss/index.xml",
     "hosts": frozenset({"www.managingmadrid.com", "managingmadrid.com"}), "teams": ("real-madrid",)},
    {"id": "bavarian_football_works", "name": "Bavarian Football Works", "url": "https://www.bavarianfootballworks.com/rss/index.xml",
     "hosts": frozenset({"www.bavarianfootballworks.com", "bavarianfootballworks.com"}), "teams": ("bayern-munich",)},
    {"id": "fear_the_wall", "name": "Fear the Wall", "url": "https://www.fearthewall.com/rss/index.xml",
     "hosts": frozenset({"www.fearthewall.com", "fearthewall.com"}), "teams": ("borussia-dortmund",)},
    {"id": "ac_milan_offside", "name": "AC Milan Offside", "url": "https://acmilan.theoffside.com/rss/index.xml",
     "hosts": frozenset({"acmilan.theoffside.com"}), "teams": ("ac-milan",)},
    {"id": "into_the_calderon", "name": "Into the Calderón", "url": "https://www.intothecalderon.com/rss/index.xml",
     "hosts": frozenset({"www.intothecalderon.com", "intothecalderon.com"}), "teams": ("atletico-madrid",)},
)

# BBC serves one football feed per club for 50 of the 96 catalog clubs; the
# other 46 have no club feed and answer 404. Only verified slugs are listed, so
# an unsupported club gets no BBC feed rather than a source that can only fail.
# Probed 2026-10-08 by fetching each slug and parsing the feed.
BBC_TEAM_SLUGS = {
    "ac-milan": "milan", "athletic-club": "athletic-bilbao", "brighton": "brighton-and-hove-albion",
    "inter": "inter-milan", "paris-saint-germain": "paris-st-germain", "rb-leipzig": "leipzig",
    "arsenal": "arsenal", "aston-villa": "aston-villa", "atalanta": "atalanta",
    "atletico-madrid": "atletico-madrid", "barcelona": "barcelona", "bayer-leverkusen": "bayer-leverkusen",
    "bayern-munich": "bayern-munich", "borussia-dortmund": "borussia-dortmund", "brentford": "brentford",
    "chelsea": "chelsea", "coventry-city": "coventry-city", "crystal-palace": "crystal-palace",
    "eintracht-frankfurt": "eintracht-frankfurt", "everton": "everton", "fiorentina": "fiorentina",
    "fulham": "fulham", "getafe": "getafe", "hoffenheim": "hoffenheim",
    "hull-city": "hull-city", "ipswich-town": "ipswich-town", "juventus": "juventus", "lazio": "lazio",
    "leeds-united": "leeds-united", "lille": "lille", "liverpool": "liverpool", "lyon": "lyon",
    "manchester-city": "manchester-city", "manchester-united": "manchester-united", "marseille": "marseille",
    "monaco": "monaco", "napoli": "napoli", "newcastle-united": "newcastle-united", "nice": "nice",
    "nottingham-forest": "nottingham-forest", "real-betis": "real-betis", "real-madrid": "real-madrid",
    "real-sociedad": "real-sociedad", "rennes": "rennes", "roma": "roma", "sevilla": "sevilla",
    "sunderland": "sunderland", "tottenham-hotspur": "tottenham-hotspur", "valencia": "valencia",
    "villarreal": "villarreal",
}

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
#
# Chinese text is the reason two entries here carry a bare place name. "米兰"
# alone is Milan the city, and it is a substring of 国际米兰 (Inter) as well as
# of AC米兰, so an article about either club matches the other's alias. Chinese
# has no word boundary to fall back on, so the short form is dropped and the
# full club name has to appear.
AMBIGUOUS_GLOBAL_ALIASES = {
    "inter": {"inter", "米兰"},
    "ac-milan": {"milan", "米兰"},
    "nice": {"nice"}, "lens": {"lens"}, "angers": {"angers"},
}
# French clubs whose names are also ordinary English words ("angers", "nice",
# "lens") are unambiguously the club inside a French-language football feed.
# The ambiguity guard exists for English-language text; applying it to a French
# publisher silently discards every Angers and Nice story there.
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
        # Removed 2026-10-08: `espn_soccer` pointed at
        # https://www.espn.com/espn/rss/soccer/news, which answers HTTP 202 with
        # a zero-byte body — it can never yield a candidate, and a permanent
        # failure occupying a source slot is worse than no source. The working
        # ESPN paths (the structured API and the per-club page) are separate and
        # unaffected. `gazzetta_football` was removed for a different reason:
        # it answers 200 with an index whose newest entry is dated 2026-03-31
        # and whose bulk is from January 2024, so every health check calls it
        # fresh while it contributes nothing. Neither host is dropped from
        # attribution: see news_source_tiers.PRESS_SOURCES.
        {"id": "marca_football", "name": "马卡报 · 足球", "kind": "rss",
         "url": "https://e00-marca.uecdn.es/rss/futbol.xml", "allowed_hosts": sorted(MARCA_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-09-24",
         "description": "西班牙马卡报官方足球订阅；仅阅读公开正文，按球队筛选。"},
        {"id": "kicker_football", "name": "踢球者 · 足球", "kind": "rss",
         "url": "https://newsfeed.kicker.de/news/aktuell", "allowed_hosts": sorted(KICKER_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-09-24",
         "description": "德国踢球者公开订阅；索引可读，正文页对自动访问返回 403，因此仅在索引命中后尝试，读不到会记为读取失败。"},
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


def verified_publisher_sources():
    """Publisher feeds read directly on 2026-10-08 and confirmed parsable.

    Every entry here was fetched with no proxy and produced both a feed and a
    readable first article. Feeds that answered 200 with a dead archive
    (Gazzetta's index stops in March 2026), refused article access (Kicker
    returns 403 on article pages), or returned a redirect shell are deliberately
    absent — a source that cannot yield citable text is not coverage.
    """
    return [
        {"id": "givemesport", "name": "GiveMeSport", "kind": "rss",
         "url": "https://www.givemesport.com/feed/", "allowed_hosts": sorted(GIVEMESPORT_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "英国综合体育媒体足球订阅；正文可直接读取。"},
        {"id": "the42", "name": "The42", "kind": "rss",
         "url": "https://www.the42.ie/feed/", "allowed_hosts": sorted(THE42_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "爱尔兰 The42 体育订阅；补充英伦以外的英语来源。"},
        {"id": "hitc", "name": "HITC Football", "kind": "rss",
         "url": "https://www.hitc.com/feed/", "allowed_hosts": sorted(HITC_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "HITC 足球订阅；转会与赛前消息按其自身来源归属呈现。"},
        {"id": "football_italia", "name": "Football Italia", "kind": "rss",
         "url": "https://www.football-italia.net/feed", "allowed_hosts": sorted(FOOTBALL_ITALIA_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "意甲英文报道订阅；补充意大利球队的英文来源。"},
        {"id": "ninety_min", "name": "90min", "kind": "rss",
         "url": "https://www.90min.com/posts.rss", "allowed_hosts": sorted(NINETY_MIN_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "综合足球媒体公开订阅；转会与赛前消息按其自身来源归属呈现。"},
        {"id": "metro_football", "name": "Metro · 足球", "kind": "rss",
         "url": "https://metro.co.uk/sport/football/feed/", "allowed_hosts": sorted(METRO_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "英国 Metro 体育足球订阅；正文与配图可直接读取。"},
        {"id": "mirror_football", "name": "镜报 · 足球", "kind": "rss",
         "url": "https://www.mirror.co.uk/sport/football/?service=rss", "allowed_hosts": sorted(MIRROR_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "英国镜报体育足球订阅；按球队筛选后阅读公开正文。"},
        {"id": "caught_offside", "name": "Caught Offside", "kind": "rss",
         "url": "https://www.caughtoffside.com/feed/", "allowed_hosts": sorted(CAUGHT_OFFSIDE_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "转会传闻类媒体公开订阅；传闻按其来源层级转述，不写成已完成事实。"},
        {"id": "hard_tackle", "name": "The Hard Tackle", "kind": "rss",
         "url": "https://www.thehardtackle.com/feed/", "allowed_hosts": sorted(HARD_TACKLE_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "国际足球媒体公开订阅；正文完整可读。"},
        {"id": "football_london", "name": "Football.London", "kind": "rss",
         "url": "https://www.football.london/all-about/arsenal?service=rss",
         "allowed_hosts": sorted(FOOTBALL_LONDON_HOSTS), "global_feed": True, "shared_index": True,
         "reviewed_at": "2026-10-08",
         "description": "伦敦地区足球媒体的球队频道订阅；按当前球队筛选，不把其他伦敦球队的消息混入。"},
        {"id": "mundo_deportivo", "name": "世界体育报 · 足球", "kind": "rss",
         "url": "https://www.mundodeportivo.com/rss/futbol", "allowed_hosts": sorted(MUNDO_DEPORTIVO_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "西班牙世界体育报足球订阅；补充西甲球队的西班牙语来源。"},
        {"id": "tuttosport", "name": "全体育 · 足球", "kind": "rss",
         "url": "https://www.tuttosport.com/rss/calcio", "allowed_hosts": sorted(TUTTOSPORT_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "意大利全体育足球订阅；补充意甲球队的意大利语来源。"},
        {"id": "record_pt", "name": "Record · 足球", "kind": "rss",
         "url": "https://www.record.pt/rss", "allowed_hosts": sorted(RECORD_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "葡萄牙 Record 体育订阅；补充葡语足球消息。"},
        # Non-English desks (2026-10-08). Each was fetched and its first article
        # read in full before being listed.
        {"id": "calciomercato", "name": "Calciomercato", "kind": "rss",
         "url": "https://feeds.footballco.com/calcio/feed/g8r5n1czq4mw7v2k",
         "allowed_hosts": sorted(CALCIOMERCATO_HOSTS), "global_feed": True, "shared_index": True,
         "reviewed_at": "2026-10-08",
         "description": "意大利转会与意甲消息订阅；正文与配图可直接读取，转会传闻按其来源归属转述。"},
        {"id": "di_marzio", "name": "Gianluca Di Marzio", "kind": "rss",
         "url": "https://www.gianlucadimarzio.com/rss", "allowed_hosts": sorted(DI_MARZIO_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "意大利知名转会记者的公开订阅；其个人报道按媒体来源呈现，不写成已确认事实。"},
        {"id": "milannews", "name": "MilanNews", "kind": "rss",
         "url": "https://www.milannews.it/rss", "allowed_hosts": sorted(MILANNEWS_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "意大利米兰球队专线媒体；按当前球队筛选，不把其他球队消息混入。"},
        {"id": "relevo", "name": "Relevo", "kind": "rss",
         "url": "https://www.relevo.com/rss/", "allowed_hosts": sorted(RELEVO_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "西班牙 Relevo 体育订阅；补充西甲球队的西班牙语来源。"},
        {"id": "diario_gol", "name": "Diario Gol", "kind": "rss",
         "url": "https://www.diariogol.com/rss/", "allowed_hosts": sorted(DIARIO_GOL_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "西班牙足球媒体订阅；转会类报道按其来源层级转述。"},
        {"id": "estadio_deportivo", "name": "Estadio Deportivo", "kind": "rss",
         "url": "https://www.estadiodeportivo.com/sitemaps/rss.xml",
         "allowed_hosts": sorted(ESTADIO_DEPORTIVO_HOSTS), "global_feed": True, "shared_index": True,
         "reviewed_at": "2026-10-08",
         "description": "西班牙塞维利亚地区体育媒体订阅；补充西甲中下游球队的本国报道。"},
        {"id": "but_football", "name": "But! Football Club", "kind": "rss",
         "url": "https://www.butfootballclub.fr/feed", "allowed_hosts": sorted(BUT_FOOTBALL_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "法国足球媒体订阅；补充法甲球队的法语来源。"},
        {"id": "maisfutebol", "name": "Maisfutebol", "kind": "rss",
         "url": "https://maisfutebol.iol.pt/rss", "allowed_hosts": sorted(MAISFUTEBOL_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "葡萄牙 Maisfutebol 订阅；补充葡语足球消息。"},
        {"id": "vi_nl", "name": "Voetbal International", "kind": "rss",
         "url": "https://www.vi.nl/rss", "allowed_hosts": sorted(VI_NL_HOSTS),
         "global_feed": True, "shared_index": True, "reviewed_at": "2026-10-08",
         "description": "荷兰足球媒体订阅；补充荷甲与荷兰球员消息。"},
    ]


def community_sources(team_id):
    """Supporter publications for one club, plus its BBC club feed.

    A supporter blog is not a weaker newspaper: it answers what happened at the
    training ground, which no official desk reports. Entries here are relayed as
    community claims and never supply a structured field.
    """
    if team_id not in TEAM_ALIASES:
        return []
    sources = []
    for feed in COMMUNITY_FEEDS:
        if team_id not in feed["teams"]:
            continue
        sources.append({"id": feed["id"], "name": feed["name"], "kind": "rss", "url": feed["url"],
                        "allowed_hosts": sorted(feed["hosts"]), "community": True, "shared_index": True,
                        "reviewed_at": "2026-10-08",
                        "description": "经核对的球迷或独立博客订阅；作为社区说法转述，不进入结构化字段。"})
    slug = BBC_TEAM_SLUGS.get(team_id)
    if slug:
        sources.append({"id": "bbc_team_" + team_id, "name": "BBC 体育 · 本队频道", "kind": "rss",
                        "url": f"https://feeds.bbci.co.uk/sport/football/teams/{slug}/rss.xml",
                        "allowed_hosts": sorted(BBC_HOSTS), "reviewed_at": "2026-10-08",
                        "description": "BBC 按俱乐部发布的专属订阅；比综合订阅更集中，仍按当前球队筛选。"})
    return sources


# Entry points that answer on an ordinary network in mainland China, where the
# reviewed Western desks above are unreachable because local DNS is
# intercepted. The file is a probe result, not a hand-written list: it holds
# only the shapes that scripts/probe-news-sources.py fetched and found entries
# in. Sources that answered but yielded nothing readable are deliberately absent
# — a permanent failure occupying a source slot is worse than no source, which
# is why `espn_soccer` and `gazzetta_football` were removed from the list above.
DOMESTIC_CHANNELS_PATH = Path(__file__).resolve().parents[1] / "public/assets/clubs/news_channels.json"


def _domestic_channels():
    try:
        payload = json.loads(DOMESTIC_CHANNELS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    channels = payload.get("channels")
    return channels if isinstance(channels, list) else []


def domestic_sources(team_id):
    """Reachable Chinese-language desks, shared by every club.

    These are general football feeds covering a competition rather than one
    club, so the same club-scope filter that applies to the global publisher
    feeds applies here.
    """
    if team_id not in TEAM_ALIASES:
        return []
    return [{**channel, "builtin": True, "global_feed": True, "shared_index": True,
             "description": channel.get("description") or "本机网络可达的中文体育栏目；按当前球队与男足一线队范围筛选。"}
            for channel in _domestic_channels()]


def domestic_hosts():
    """Every host a domestic channel needs, for the allowlist that grants reach."""
    return {host for channel in _domestic_channels() for host in (channel.get("allowed_hosts") or ()) if host}


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
    # A French feed's summary often cites a former club only as biographical
    # background, so headlines identify the subject more reliably there. "OM" is
    # Marseille's established shorthand in French football writing.
    value = (str(entry.get("title") or "") if host in FRENCH_PUBLISHER_HOSTS else
             " ".join(str(entry.get(key) or "") for key in ("title", "description")))
    if host in FRENCH_PUBLISHER_HOSTS and team_id == "marseille" and contains(value, "om"):
        return True
    # South American clubs can share the short names of European clubs. Do not
    # assign their stories to the tracked club without a distinct identity.
    for other in OTHER_CLUB_IDENTITIES.get(team_id, ()):
        value = re.sub(re.escape(other), " ", value, flags=re.I)
    ambiguous = AMBIGUOUS_GLOBAL_ALIASES.get(team_id, ())
    # A French club's short name collides with an ordinary English word, not
    # with a French one. Inside a French-language feed "Angers" is the club, so
    # the English-text guard is not applied there.
    if host in FRENCH_PUBLISHER_HOSTS:
        ambiguous = set()
    aliases = (*TEAM_ALIASES.get(team_id, ()), *EXPLICIT_GLOBAL_ALIASES.get(team_id, ()))
    return any(contains(value, alias) for alias in aliases if alias not in ambiguous)
