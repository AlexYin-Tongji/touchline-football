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
