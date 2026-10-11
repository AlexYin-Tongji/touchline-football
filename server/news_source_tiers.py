"""Reviewed attribution for news publishers; this grants no network access.

Only the checked-in club catalog and the fixed publishers below can grant a
tier. User profiles, custom sources and the transport allowlist cannot do so.
ESPN retains its provider registration as DATASET, including structured claims.
"""
from urllib.parse import urlsplit

from .club_library import CLUBS
from . import provenance
from .provenance import SourceTier


PRESS_SOURCES = (
    ("news_bbc", "BBC 体育", ("bbc.co.uk", "bbc.com", "feeds.bbci.co.uk")),
    ("news_sky", "天空体育", ("skysports.com",)),
    ("news_guardian", "卫报体育", ("theguardian.com",)),
    ("news_rmc", "RMC Sport", ("rmcsport.bfmtv.com",)),
    ("news_as", "阿斯报", ("as.com", "feeds.as.com")),
    ("news_tyc", "TyC Sports", ("tycsports.com",)),
    ("news_marca", "马卡报", ("marca.com", "e00-marca.uecdn.es")),
    ("news_gazzetta", "米兰体育报", ("gazzetta.it",)),
    ("news_kicker", "踢球者", ("kicker.de", "newsfeed.kicker.de")),
    ("news_tmw", "TuttoMercatoWeb", ("tuttomercatoweb.com",)),
    ("news_footmercato", "Foot Mercato", ("footmercato.net",)),
    # Added 2026-10-08 alongside the feed definitions in news_sources.py. A
    # publisher that is read but never registered would reach the reader as an
    # unclassified source, which hides the one thing this page exists to show.
    ("news_football_italia", "Football Italia", ("football-italia.net",)),
    ("news_90min", "90min", ("90min.com",)),
    ("news_metro", "Metro", ("metro.co.uk",)),
    ("news_mirror", "镜报", ("mirror.co.uk",)),
    ("news_caught_offside", "Caught Offside", ("caughtoffside.com",)),
    ("news_hard_tackle", "The Hard Tackle", ("thehardtackle.com",)),
    ("news_football_london", "Football.London", ("football.london",)),
    ("news_mundo_deportivo", "世界体育报", ("mundodeportivo.com",)),
    ("news_tuttosport", "全体育", ("tuttosport.com",)),
    ("news_record", "Record", ("record.pt",)),
    ("news_givemesport", "GiveMeSport", ("givemesport.com",)),
    ("news_the42", "The42", ("the42.ie",)),
    ("news_hitc", "HITC", ("hitc.com",)),
    # Non-English desks added 2026-10-08. Attribution is kept for `gazzetta.it`
    # above and `kicker.de` even though neither feed is currently listed as a
    # source: a URL from either can still reach the reader through a
    # user-configured source or a stored report, and an unknown host there would
    # display as unclassified.
    ("news_calciomercato", "Calciomercato", ("footballco.com",)),
    ("news_di_marzio", "Gianluca Di Marzio", ("gianlucadimarzio.com",)),
    ("news_milannews", "MilanNews", ("milannews.it",)),
    ("news_relevo", "Relevo", ("relevo.com",)),
    ("news_diario_gol", "Diario Gol", ("diariogol.com",)),
    ("news_estadio_deportivo", "Estadio Deportivo", ("estadiodeportivo.com",)),
    ("news_but_football", "But! Football Club", ("butfootballclub.fr",)),
    ("news_maisfutebol", "Maisfutebol", ("maisfutebol.iol.pt",)),
    ("news_vi_nl", "Voetbal International", ("vi.nl",)),
    # Chinese-language desks, added 2026-10-11 with the channel declarations in
    # news_channels.json. They are read on networks where the desks above cannot
    # be reached; registering them keeps a readable source from displaying as
    # unclassified.
    ("news_netease", "网易体育", ("163.com", "sports.163.com")),
)

# Supporter publications. They are community sources whatever the quality of
# the writing, so they are registered at the community tier from the same list
# the feeds are declared in.
COMMUNITY_SOURCES = (
    ("community_arseblog", "Arseblog", ("arseblog.news",)),
    ("community_barca_blaugranes", "Barca Blaugranes", ("barcablaugranes.com",)),
    ("community_managing_madrid", "Managing Madrid", ("managingmadrid.com",)),
    ("community_bavarian_football_works", "Bavarian Football Works", ("bavarianfootballworks.com",)),
    ("community_fear_the_wall", "Fear the Wall", ("fearthewall.com",)),
    ("community_ac_milan_offside", "AC Milan Offside", ("acmilan.theoffside.com",)),
    ("community_into_the_calderon", "Into the Calderón", ("intothecalderon.com",)),
)


def _official_sources(clubs):
    for club in clubs:
        official_host = (urlsplit(club.get("official_url") or "").hostname or "").lower().removeprefix("www.")
        for source in club.get("research_sources", []):
            host = (urlsplit(source.get("url") or "").hostname or "").lower().removeprefix("www.")
            if host and host == official_host and str(source.get("id") or "").startswith("official_"):
                yield source["id"], host, club.get("name") or club["id"]


OFFICIAL_SOURCES = tuple(_official_sources(CLUBS))


def load():
    """Idempotently register exact reviewed hosts and their www aliases only."""
    for identity, host, name in OFFICIAL_SOURCES:
        previous = provenance.registered(host)
        if not previous or previous["tier"] is not SourceTier.OFFICIAL:
            provenance.register(identity, host, SourceTier.OFFICIAL, name=name + "官网", kinds=("news",),
                                note="版本控制球队目录中，与该俱乐部官网同域名的官方消息来源。")
    for identity, name, hosts in PRESS_SOURCES:
        for host in hosts:
            previous = provenance.registered(host)
            if not previous or previous["tier"] is not SourceTier.PRESS:
                provenance.register(identity, host, SourceTier.PRESS, name=name, kinds=("news",),
                                    note="已审核的固定足球新闻媒体；仅作来源归属，不授予阅读或联网范围。")
    for identity, name, hosts in COMMUNITY_SOURCES:
        for host in hosts:
            previous = provenance.registered(host)
            if not previous or previous["tier"] is not SourceTier.COMMUNITY:
                provenance.register(identity, host, SourceTier.COMMUNITY, name=name, kinds=("news", "opinion"),
                                    note="经核对的球迷或独立博客；按社区说法转述，不提升为媒体层级，也不进入结构化字段。")
