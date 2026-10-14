"""Portrait and club-affiliation providers, wrapping the reviewed resolvers.

Two things are being fixed by giving these sources a registry entry rather than
leaving them as bare functions.

The first is tier. A Wikimedia file carries a verified free licence; a community
database does not. That difference used to be implied by which module ran first
— Wikimedia was "the preferred source" and ran before the community one — so the
order of two function calls was carrying a licence judgement. It is now stated:
Wikimedia is a DATASET source, the community database is a COMMUNITY source, and
both may answer.

The second is a mistake this project made about the community database. Its club
field was recorded as unreliable because it named a club other than the one the
roster held. At least one such record was a loan: a player contracted to one
club and playing for another is at both, so the two records were never in
conflict — the field simply does not say which relationship it means. That is a
semantic gap, not bad data, and it is why ``club`` below returns the value with
the relation left explicitly unknown rather than discarding it.

Neither provider decides which club is "correct". They return claims and let
``provenance`` keep whatever disagrees.
"""
from __future__ import annotations

from ..provenance import Relation, SourceTier
from .base import BaseProvider, Capability, Cost, Latency, ProviderError, provider


def _portrait_claims(store, result, source_id, *, team_id, fetched):
    """Turn one resolver result into a claim, or explain why there is none."""
    from .. import provenance as prov
    if not isinstance(result, dict) or result.get("status") != "completed":
        reason = (result or {}).get("photo_reason") or "来源没有可展示的照片。"
        if result is None:
            reason = "来源没有返回结果。"
        return None, reason
    url = result.get("photo_url")
    if not url:
        return None, "来源没有可展示的照片。"
    source = {"id": source_id, "url": result.get("photo_file_page") or result.get("photo_source_url") or url,
              "name": result.get("photo_credit") or source_id}
    claim = prov.claim_from(source, "photo_url", url, evidence_id="", fetched_at=fetched)
    return {"claim": claim, "credit": result.get("photo_credit", ""),
            "license": result.get("photo_license", ""),
            "file_page": result.get("photo_file_page", ""),
            "match": result.get("photo_match", "")}, reason


@provider(
    {"id": "thesportsdb", "name": "TheSportsDB 社区球员资料", "tier": SourceTier.COMMUNITY,
     "domains": (".thesportsdb.com",), "reviewed_at": "2026-09-24"},
    {Capability.PERSON, Capability.IMAGE},
    cost=Cost.LIGHT, latency=Latency.SLOW,
    note="球迷维护的社区数据库。俱乐部字段未声明关系类型，不参与身份判定。姓名搜索已退出默认照片核对；本提供者不再发出检索。",
)
class TheSportsDbProvider(BaseProvider):
    """Community club value, with portrait name-search retired.

    The club claim stays: it records an unstated relation and does not decide
    identity. Portrait lookup by name is the failed path, and the free API
    cannot be asked for an ESPN id, so ``portrait`` returns a gap and does
    not call the publisher.
    """

    min_interval = 1.1

    async def portrait(self, player, team_id, *, force=False):
        if self.store is None:
            raise ProviderError("读取照片需要存储。", code="no_store")
        return self.unavailable(
            Capability.IMAGE,
            "免费社区库不能按球员编号反查，姓名搜索已退出，本提供者不会发出检索。",
            retryable=False)

    def club(self, record, *, fetched_at=None):
        """The publisher's club value, with the relation deliberately unstated.

        This value may name a parent club or a loan club and does not say which.
        Returning it as an ordinary current-club claim would assert something the
        source never claimed, so it is returned separately and is never allowed
        to decide identity.
        """
        from .. import provenance as prov
        value = record.get("strTeam") if isinstance(record, dict) else None
        if not value:
            return None
        source = {"id": self.source.id, "url": "https://www.thesportsdb.com/", "name": self.source.name}
        claim = prov.claim_from(source, "club_id", value, fetched_at=fetched_at,
                                note="来源未声明这是母队、租借队还是历史球队，因此不参与身份判定。")
        return {"claim": claim, "relation": "unstated"}


@provider(
    {"id": "wikimedia", "name": "维基媒体（自由许可）", "tier": SourceTier.DATASET,
     "domains": (".wikipedia.org", ".wikidata.org", ".wikimedia.org"), "reviewed_at": "2026-09-24"},
    {Capability.PERSON, Capability.IMAGE},
    cost=Cost.LIGHT, latency=Latency.SLOW,
    note="自由许可图片与结构化球员资料；按完整姓名、足球运动员身份与 Wikidata 现属俱乐部核对。",
)
class WikimediaProvider(BaseProvider):
    """Free-licensed portraits and identity, subject to a licence check."""

    min_interval = 2.2

    async def portrait(self, player, team_id, *, force=False):
        from ..wikimedia_portraits import resolve_portrait
        if self.store is None:
            raise ProviderError("读取照片需要存储。", code="no_store")
        await self.pacing.wait()
        try:
            result = await resolve_portrait(self.store, player, team_id, force=force)
        except Exception as exc:
            return self.unavailable(Capability.IMAGE, str(exc) or "维基媒体暂时不可达。", retryable=True)
        item, reason = _portrait_claims(self.store, result, self.source.id, team_id=team_id,
                                        fetched=(result or {}).get("fetched_at"))
        if item is None:
            return self.envelope(Capability.IMAGE, gaps=[{"source_id": self.source.id, "message": reason,
                                                          "retryable": bool((result or {}).get("retryable"))}])
        return self.envelope(Capability.IMAGE, items=[item])

    def club_relation(self, relation):
        """Map a resolver's club verdict onto the shared vocabulary."""
        try:
            return Relation(str(relation))
        except ValueError:
            return Relation.CURRENT_CLUB


def portrait_providers():
    """Every registered source that can supply a portrait, strongest first."""
    from .base import sources
    rows = [row for row in sources(capability=Capability.IMAGE)]
    return rows
