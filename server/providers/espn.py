"""ESPN's public structured endpoints, as one registered dataset source.

This adapter delegates to the existing ``team_center`` and ``league_data``
pipelines rather than reimplementing their parsing: those paths carry the
season-membership checks that stop a stale squad from overwriting a current
one, and duplicating them here would be the fastest way to lose them.

What it adds is the part those pipelines cannot express — *when* each value was
true, *how fast* that kind of value turns over, and what a disagreement with
another source would look like. A squad snapshot becomes field-level claims so
a club list from a second provider can sit beside it instead of replacing it.
"""
from __future__ import annotations

from .. import provenance as prov
from ..provenance import SourceTier
from .base import BaseProvider, Capability, Latency, ProviderError, provider

ESPN_HOSTS = (".espn.com", ".espn.co.uk")

# Fields this provider is allowed to speak about, and the claim name each maps
# to. Keeping the mapping explicit means a provider change cannot silently start
# writing a field the rest of the system does not expect.
ROSTER_FIELDS = {
    "name": "name", "number": "number", "position": "position",
    "position_group": "position_group", "nationality": "nationality",
    "birth_date": "birth_date", "height_cm": "height_cm", "weight_kg": "weight_kg",
    "team_id": "team_id", "season_id": "season_id", "stats": "stats",
}


@provider(
    {"id": "espn", "name": "ESPN", "tier": SourceTier.DATASET, "domains": ESPN_HOSTS,
     "reviewed_at": "2026-09-12"},
    {Capability.SQUAD, Capability.FIXTURE, Capability.STANDINGS, Capability.PERSON, Capability.NEWS},
    latency=Latency.NORMAL,
    note="公开结构化接口；阵容与赛程按赛季成员关系校验，缺少可核对目录时不覆盖已有快照。",
)
class EspnProvider(BaseProvider):
    """Read-only access to the snapshot the maintenance pipeline already keeps."""

    def _season(self, team_id, year=None):
        from ..team_center import team_season_context
        if self.store is None:
            raise ProviderError("读取球队资料需要存储。", code="no_store")
        try:
            return team_season_context(self.store, team_id, year)
        except Exception as exc:
            raise ProviderError(str(exc) or "球队赛季资料不可用。", code="season_unavailable") from None

    def _kickoff(self, team_id, season_id):
        """The next fixture, used to decide how urgent a squad list is.

        Without it a squad list is rechecked on a flat daily clock even in the
        hours before kickoff, which is exactly when it changes.
        """
        from ..league_data import team_schedule_key
        from ..ingestion import published_time
        from datetime import datetime, timezone
        state = self.store.get_meta(team_schedule_key(season_id, team_id), {})
        matches = state.get("snapshot_match_ids") or []
        stamps = []
        for match_id in matches:
            match = self.store.get("league_matches", match_id) or self.store.get("matches", match_id)
            kickoff = (match or {}).get("kickoff")
            if kickoff:
                stamps.append(kickoff)
        upcoming = sorted(stamp for stamp in stamps if _in_future(stamp))
        return upcoming[0] if upcoming else None

    def squad(self, team_id, year=None, *, now=None):
        """The stored squad as claims, with the freshness each field deserves.

        Nothing is fetched here: reading the alert state must never cause a
        network request, so the caller decides when to spend one.
        """
        _, _, season, _ = self._season(team_id, year)
        from ..team_center import _snapshot_id
        snapshot = self.store.get("team_snapshots", _snapshot_id(team_id, season["id"])) or {}
        if not snapshot.get("roster"):
            return self.unavailable(Capability.SQUAD, "还没有可核对的阵容快照。")
        fetched = snapshot.get("fetched_at")
        kickoff = self._kickoff(team_id, season["id"])
        source = self._source_record(team_id, season)
        items = []
        for player in snapshot["roster"]:
            claims = {}
            for field, claim_field in ROSTER_FIELDS.items():
                value = player.get(field)
                if value in (None, "", {}):
                    continue
                claims[claim_field] = prov.claim_from(
                    source, claim_field, value, evidence_id="", fetched_at=fetched,
                    kickoff=kickoff, now=fetched,
                    note="" if field != "team_id" else "球队归属来自赛季成员目录。",
                )
            items.append({"player_id": player.get("id"), "provider_id": player.get("provider_id"),
                          "claims": claims})
        return self.envelope(Capability.SQUAD, items=items, cached=True,
                             notices=[f"阵容快照采集时间：{fetched or '未知'}。"])

    def fixtures(self, team_id, year=None):
        from ..team_center import team_context_payload
        payload = team_context_payload(self.store, team_id, year)
        matches = payload.get("matches") or []
        if not matches:
            return self.unavailable(Capability.FIXTURE, "还没有可核对的赛程资料。")
        fetched = payload.get("updated_at")
        source = self._source_record(team_id, None)
        items = []
        for match in matches:
            claims = {}
            for field in ("kickoff", "status", "result", "competition"):
                value = match.get(field)
                if value in (None, ""):
                    continue
                claims[field] = prov.claim_from(source, field, value, fetched_at=fetched, now=fetched)
            items.append({"match_id": match.get("id"), "claims": claims})
        return self.envelope(Capability.FIXTURE, items=items, cached=True)

    def standings(self, team_id, year=None):
        from ..team_center import center_view
        view = center_view(self.store, team_id, year)
        rows = view.get("standings") or []
        if not rows:
            return self.unavailable(Capability.STANDINGS, "还没有可核对的积分榜。")
        fetched = view.get("updated_at")
        source = self._source_record(team_id, None)
        items = []
        for row in rows:
            items.append({"team_id": row.get("team_id"), "rank": row.get("rank"),
                          "claim": prov.claim_from(source, "standings", row.get("rank"),
                                                  fetched_at=fetched, now=fetched)})
        return self.envelope(Capability.STANDINGS, items=items, cached=True)

    def person(self, provider_id, name="", team_id=""):
        """Identity for one player, without asserting anything we were not told."""
        if not str(provider_id or "").isdigit():
            return self.unavailable(Capability.PERSON, "球员编号无效。")
        source = self._source_record(team_id or "espn", None)
        claim = prov.claim_from(source, "name", name, fetched_at=prov.iso(None),
                                note=f"ESPN 球员编号 {provider_id}。")
        return self.envelope(Capability.PERSON, items=[{"provider_id": provider_id, "claim": claim}])

    def _source_record(self, team_id, season):
        """The reviewed registration this provider speaks under."""
        url = f"https://www.espn.com/soccer/team/_/id/{team_id}"
        return {"id": "espn", "url": url, "name": "ESPN"}


def _in_future(stamp):
    from datetime import datetime, timezone
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    return parsed > datetime.now(timezone.utc)
