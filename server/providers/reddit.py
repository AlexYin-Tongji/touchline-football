"""Supporter communities: what people are saying, attributed and never promoted.

A supporters' forum answers questions no official channel will. A player is
spotted at a training ground, a journalist's line is argued over, an away end
reports what the manager did after the whistle. That material is worth carrying
and it is worth carrying *as what it is*: an observation by people with no
editorial responsibility.

Three rules follow, and they are enforced here rather than in a prompt.

Community volume orders community items among themselves and never lifts one
into a stronger tier. A post with twelve thousand upvotes is still relayed as a
community claim, because popularity is not verification — it measures agreement,
and agreement about a rumour is agreement about a rumour.

Nothing found on a forum is written into a structured field. Supporter posts
carry ``opinion`` and ``news``; they never supply a squad number, a birth date
or a transfer status, because those fields are the ones a later reader would
treat as settled.

A failure is reported as a gap. Reddit's public JSON endpoints change shape and
rate-limit, and a caller that cannot tell "the request failed" from "nobody
posted about this" will show an outage as silence.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlencode, urlsplit

from .. import provenance as prov
from ..provenance import SourceTier
from .base import BaseProvider, Capability, Cost, Latency, ProviderError, provider

REDDIT_HOSTS = (".reddit.com",)
# Public JSON endpoints. The API prefix is used rather than the site prefix
# because it returns stable JSON without the page shell.
REDDIT_API = "https://www.reddit.com"
MAX_JSON_BYTES = 2 * 1024 * 1024


@provider(
    {"id": "reddit", "name": "Reddit 足球社区", "tier": SourceTier.COMMUNITY, "domains": REDDIT_HOSTS,
     "reviewed_at": "2026-09-28"},
    {Capability.NEWS, Capability.OPINION},
    cost=Cost.LIGHT, latency=Latency.NORMAL,
    note="球迷讨论。认同量只用于社区来源之间的排序，不提升层级，也不进入结构化字段。",
)
class RedditProvider(BaseProvider):
    """Supporter posts as attributed claims, with agreement recorded."""

    min_interval = 1.0

    # A subreddit is a community, not a club. Which club a thread is about is
    # decided by its own title and text, exactly as a publisher feed is
    # filtered, so a post about a former player is not filed under this club.
    DEFAULT_SUBREDDITS = ("soccer",)

    async def posts(self, team_id, query="", *, subreddits=None, limit=25, force=False, now=None):
        from ..ingestion import IngestionError, fetch_document, source_error_details
        from ..research_transport import shared_request
        if self.store is None:
            raise ProviderError("读取社区来源需要存储。", code="no_store")
        limit = max(1, min(100, int(limit or 25)))
        names = tuple(subreddits or self.DEFAULT_SUBREDDITS)
        url = f"{REDDIT_API}/r/{'+'.join(names)}/search.json?" + urlencode(
            {"q": str(query or team_id), "restrict_sr": 1, "sort": "new", "limit": limit, "raw_json": 1})

        async def factory():
            body, final_url, content_type = await fetch_document(
                url, allowed_hosts=_allowed_reddit_hosts(), max_body=MAX_JSON_BYTES,
                content_types=("application/json", "text/json", "text/plain", "text/html"))
            parts = urlsplit(final_url)
            if parts.scheme != "https" or not _is_reddit_host(parts.hostname) or parts.fragment:
                raise IngestionError("社区来源响应地址异常，未采用。")
            try:
                payload = json.loads(body)
            except (UnicodeError, ValueError, RecursionError):
                raise IngestionError("社区来源资料无法读取。") from None
            if not isinstance(payload, dict):
                raise IngestionError("社区来源返回了无法识别的格式。")
            return payload

        await self.pacing.wait()
        try:
            payload = await shared_request(self.store, ("reddit", url), "www.reddit.com", factory, force=force)
        except Exception as exc:
            details = source_error_details(exc)
            return self.unavailable(Capability.OPINION, details.get("message") or "社区来源暂时不可达。",
                                    retryable=details.get("code") not in {"blocked_redirect", "response_too_large"})

        items, gaps = self._read(payload, team_id, now=now)
        return self.envelope(Capability.OPINION, items=items, gaps=gaps)

    def _read(self, payload, team_id, *, now=None):
        children = ((payload.get("data") or {}).get("children")) if isinstance(payload, dict) else None
        if not isinstance(children, list):
            return [], [{"source_id": self.source.id, "message": "社区来源结构已变化，本次没有可读条目。",
                         "retryable": True}]
        source = {"id": self.source.id, "url": f"{REDDIT_API}/r/soccer", "name": self.source.name}
        items = []
        for child in children[:60]:
            if not isinstance(child, dict):
                continue
            data = child.get("data") or {}
            if not isinstance(data, dict):
                continue
            title = _text(data.get("title"), 300)
            permalink = data.get("permalink")
            if not title or not isinstance(permalink, str):
                continue
            link = f"{REDDIT_API}{permalink}" if permalink.startswith("/") else permalink
            if not _is_reddit_host(urlsplit(link).hostname):
                continue
            created = _timestamp(data.get("created_utc"))
            # Agreement, not accuracy. Two numbers are recorded separately
            # because a thousand upvotes and four comments describe different
            # things, and adding them would hide which one happened.
            score = _count(data.get("score"))
            comments = _count(data.get("num_comments"))
            claim = prov.claim_from(
                source, "opinion", title, evidence_id="", fetched_at=prov.iso(now),
                observed_at=created, corroboration=score,
                note=f"社区帖；评论数 {comments if comments is not None else '未知'}。")
            items.append({"id": str(data.get("id") or ""), "url": link, "title": title,
                          "author_flair": _text(data.get("link_flair_text"), 80),
                          "obs_created_at": created, "score": score, "comments": comments,
                          "claim": claim, "is_self": bool(data.get("is_self"))})
        return items, []

    def verify_identity(self, item, team_id):
        """Whether a thread's own text names this club.

        Subreddit membership is not club identity: r/soccer carries every club
        and a rival's thread can mention this one in passing. Only the post's
        own words establish the subject, the same rule the publisher feeds use.
        """
        from ..events import contains
        from ..club_library import TEAM_ALIASES
        value = " ".join(str(item.get(key) or "") for key in ("title", "selftext"))
        aliases = TEAM_ALIASES.get(team_id) or ()
        return any(contains(value, alias) for alias in aliases if alias)


def _allowed_reddit_hosts():
    return {"www.reddit.com", "reddit.com", "old.reddit.com", "oauth.reddit.com"}


def _is_reddit_host(host):
    host = (host or "").lower()
    return host in _allowed_reddit_hosts() or host.endswith(".reddit.com")


def _text(value, maximum):
    return " ".join(str(value or "").split())[:maximum]


def _count(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 10_000_000 else None


def _timestamp(value):
    from datetime import datetime, timezone
    try:
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return moment.isoformat()
