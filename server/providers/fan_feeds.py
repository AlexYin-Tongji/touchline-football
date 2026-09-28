"""Supporter blogs and forums that publish a feed.

These sit between a publisher and a message board. They are written by people
with no editorial desk behind them, so they are a community source whatever the
quality of the writing, and they are relayed that way. What they add over a
thread is a byline and a feed: something that can be followed without scraping,
and whose entries are dated by their author.

A feed carries no agreement signal, so nothing here sets ``corroboration``. That
is deliberate: inventing a number to make a blog post look corroborated is the
one thing that would let volume masquerade as evidence.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from ..provenance import SourceTier
from .base import BaseProvider, Capability, Cost, Latency, ProviderError, provider

# Reviewed supporter publications, one entry per feed. Adding a feed is a
# reviewed change: the host is granted a tier by registering it here.
FEEDS = (
    {"id": "arseblog", "name": "Arseblog", "url": "https://arseblog.news/feed/",
     "host": "arseblog.news", "teams": ("arsenal",)},
    {"id": "the_busby_babe", "name": "The Busby Babe", "url": "https://thebusbybabe.com/feed/",
     "host": "thebusbybabe.com", "teams": ("manchester_united",)},
    {"id": "barca_blaugranes", "name": "Barca Blaugranes", "url": "https://www.barcablaugranes.com/rss/index.xml",
     "host": "barcablaugranes.com", "teams": ("barcelona",)},
    {"id": "we_aint_got_no_history", "name": "We Ain't Got No History", "url": "https://weaintgotnohistory.sbnation.com/rss/current",
     "host": "weaintgotnohistory.sbnation.com", "teams": ("chelsea",)},
    {"id": "managing_madrid", "name": "Managing Madrid", "url": "https://www.managingmadrid.com/rss/index.xml",
     "host": "managingmadrid.com", "teams": ("real_madrid",)},
    {"id": "black_and_white_and_red", "name": "Black & White & Red All Over", "url": "https://www.blackwhitereadallover.com/rss/index.xml",
     "host": "blackwhitereadallover.com", "teams": ("juventus",)},
)
FEED_HOSTS = tuple(sorted({entry["host"] for entry in FEEDS}))
FEED_IDS = {entry["id"]: entry for entry in FEEDS}
MAX_BODY = 2 * 1024 * 1024


def _register_feed_hosts():
    """Grant each reviewed feed host the community tier.

    One provider spans many publications, so the tier of each host is granted
    from the reviewed list rather than left to a single declaration. A host that
    is not in ``FEEDS`` is never granted anything, which is what keeps an
    unreviewed blog from being read as a supporter source.
    """
    from .. import provenance as prov
    for entry in FEEDS:
        prov.register(entry["id"], entry["host"], SourceTier.COMMUNITY, name=entry["name"],
                      kinds=["news", "opinion"], note="经核对的球迷或独立博客订阅。")


@provider(
    {"id": "fan_feeds", "name": "球迷博客订阅", "tier": SourceTier.COMMUNITY, "domains": (".sbnation.com",),
     "reviewed_at": "2026-09-28"},
    {Capability.NEWS, Capability.OPINION},
    cost=Cost.LIGHT, latency=Latency.NORMAL,
    note="球迷与独立博客。写入社区层级，不带认同量，不进入结构化字段。",
)
class FanFeedProvider(BaseProvider):
    """Reviewed supporter publications, read one feed at a time."""

    min_interval = 0.5

    def feeds_for(self, team_id):
        return [entry for entry in FEEDS if team_id in entry["teams"]]

    async def entries(self, team_id, *, limit=20, force=False, now=None):
        from ..ingestion import IngestionError, source_error_details
        from ..research_transport import shared_request
        if self.store is None:
            raise ProviderError("读取订阅需要存储。", code="no_store")
        feeds = self.feeds_for(team_id)
        if not feeds:
            # Club scope is deliberate: an unreviewed feed is not read just
            # because a club id happens to match.
            return self.envelope(Capability.OPINION, gaps=[
                {"source_id": self.source.id,
                 "message": "这支球队还没有经核对的球迷订阅；可在来源配置中自行添加。",
                 "retryable": False}])
        items, gaps = [], []
        for entry in feeds:
            try:
                items.extend(await self._read_feed(entry, limit=limit, force=force, now=now))
            except Exception as exc:
                details = source_error_details(exc)
                gaps.append({"source_id": self.source.id, "feed": entry["id"],
                             "message": f"{entry['name']}：{details.get('message') or '暂时不可达'}",
                             "retryable": True})
        return self.envelope(Capability.OPINION, items=items, gaps=gaps)

    async def _read_feed(self, feed, *, limit, force, now):
        from ..ingestion import IngestionError, fetch_document, html_text, published_time
        from ..research_transport import shared_request
        url = feed["url"]

        async def factory():
            body, final_url, _ = await fetch_document(url, allowed_hosts={feed["host"]}, max_body=MAX_BODY,
                                                     content_types=("xml", "text/", "xhtml"))
            if urlsplit(final_url).hostname != feed["host"]:
                raise IngestionError("订阅地址跳转到了其他站点，未采用。")
            return body, final_url

        await self.pacing.wait()
        body, _ = await shared_request(self.store, ("fan_feed", url), feed["host"], factory, force=force)
        source = {"id": self.source.id, "url": url, "name": feed["name"]}
        from .. import provenance as prov
        items = []
        for entry in _feed_entries(body)[:max(1, min(60, limit))]:
            title = html_text(entry.get("title") or "")[:300]
            link = entry.get("link") or ""
            if not title or not _same_publication(link, feed):
                continue
            claim = prov.claim_from(source, "opinion", title, evidence_id="", fetched_at=prov.iso(now),
                                    observed_at=entry.get("published_at") or "",
                                    note="球迷博客文章，无编辑责任。")
            items.append({"feed": feed["id"], "url": link, "title": title,
                          "summary": html_text(entry.get("description") or "")[:600],
                          "observed_at": entry.get("published_at") or "", "claim": claim})
        return items


def _feed_entries(body):
    from ..ingestion import html_text, published_time
    from xml.etree import ElementTree
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        from ..ingestion import IngestionError
        raise IngestionError("不支持带实体声明的订阅源")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        from ..ingestion import IngestionError
        raise IngestionError("球迷订阅不是有效 XML") from None
    result = []
    for node in (item for item in root.iter() if item.tag.rsplit("}", 1)[-1] in {"item", "entry"}):
        values = {}
        link = None
        for child in node:
            key = child.tag.rsplit("}", 1)[-1]
            values[key] = "".join(child.itertext()).strip()
            if key == "link" and child.get("rel", "alternate") == "alternate":
                link = child.get("href") or values[key]
        result.append({"title": values.get("title", ""), "description": values.get("description") or values.get("summary") or "",
                       "link": link, "published_at": published_time(values.get("pubDate") or values.get("published") or values.get("updated"))})
    return result


def _same_publication(link, feed):
    """An article link must belong to the publication whose feed carried it."""
    if not isinstance(link, str) or not link:
        return False
    try:
        host = (urlsplit(link).hostname or "").lower()
    except ValueError:
        return False
    return host == feed["host"] or host.endswith("." + feed["host"].split(".", 1)[-1])


# Grant each reviewed feed host its tier when this module is imported. The class
# decorator above registers the provider; the individual publications are
# registered here because one provider spans many domains.
_register_feed_hosts()
