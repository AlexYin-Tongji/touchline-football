"""Inspect a fixed cross-league cohort; --live opts into bounded public reads.

Uses a new temporary Store, no user credentials or paid models. Reports contain
counts and diagnostics only; external article text is never written to disk.
Success rates use per-team denominators, not pooled successes of popular clubs.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.club_library import CLUBS
from server.build_info import VERSION, application_id
from server.seed import seed
from server.storage import Store

COHORT = ("manchester-united", "brighton", "atletico-madrid", "getafe", "napoli", "udinese",
          "borussia-dortmund", "mainz", "lille", "angers", "everton", "celta-vigo", "bologna", "augsburg", "le-havre")


async def inspect(store, team_id, gate, *, timeout):
    from server import research, team_center
    async with gate:
        started = time.monotonic()
        member = next((row for row in store.list("league_memberships") if row["team_id"] == team_id), {})
        result = {"team_id": team_id, "league_id": member.get("league_id"), "roster": {}, "news": {}, "errors": []}
        async def roster():
            try:
                await team_center.sync_roster(store, team_id, force=True)
                snapshot = next((row for row in store.list("team_snapshots") if row.get("team_id") == team_id), {})
                players = snapshot.get("roster", [])
                result["roster"] = {"players": len(players), "photos": sum(bool(row.get("photo_url")) for row in players),
                                    "names_with_sources": sum(bool(row.get("name_zh") and row.get("name_zh_source_url")) for row in players),
                                    "source_url": snapshot.get("source_url"), "season_id": snapshot.get("season_id"),
                                    "source_method": snapshot.get("source_method"), "success": bool(players)}
            except Exception:
                result["roster"] = {"success": False, "error": "roster_unavailable"}
        async def news():
            try:
                found = await research.collect_team(store, team_id, limit=2, max_attempts=4, max_age_days=7)
                result["news"] = {key: found.get(key) for key in ("coverage", "attempted_reads", "counts", "sources", "source_errors")}
                result["news"].update(candidates=len(found.get("candidates", [])), readable=len(found.get("evidence_ids", [])),
                                      scope_rejected_count=found.get("scope_rejected_count", 0),
                                      success=bool(found.get("evidence_ids")), model_requests=0)
                result["news"]["observations"] = [
                    {key: (store.get("evidence", identity) or {}).get(key) for key in
                     ("source_id", "source_name", "url", "published_at", "published_date", "publication_precision", "content_status", "article_body_bound", "source_scope")}
                    for identity in found.get("evidence_ids", [])]
            except Exception:
                result["news"] = {"success": False, "error": "discovery_unavailable", "model_requests": 0}
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(roster(), news())
        except TimeoutError:
            result["errors"].append("team_budget_exhausted")
        result["seconds"] = round(time.monotonic() - started, 2)
        return result


def cohort(teams):
    known = {row["id"] for row in CLUBS}
    unknown = set(teams) - known
    if unknown:
        raise ValueError("Unknown team IDs: " + ", ".join(sorted(unknown)))
    return list(dict.fromkeys(teams))


async def run(args):
    teams = cohort(args.teams or COHORT)
    metadata = {"checked_at": datetime.now(timezone.utc).isoformat(), "live": args.live,
                "version": VERSION, "application_fingerprint": application_id(),
                "catalog_teams": len(CLUBS), "cohort": teams, "temporary_database": True,
                "model_requests": 0, "user_data_modified": False,
                "limits": {"team_timeout_seconds": args.timeout, "team_concurrency": 2,
                           "article_success_target": 2, "article_attempts": 4, "window_days": 7}}
    if not args.live:
        return {**metadata, "notice": "Plan only; no network requests or measured success rates."}
    with tempfile.TemporaryDirectory(prefix="touchline-generalization-") as folder:
        store = Store(Path(folder) / "acceptance.sqlite3")
        try:
            seed(store)
            store.save_settings({"followed_teams": [], "spoiler_free": False})
            gate = asyncio.Semaphore(2)
            async def measured(team):
                row = await inspect(store, team, gate, timeout=args.timeout)
                print(json.dumps({"team": team, "roster": row["roster"].get("players", 0),
                                  "news": row["news"].get("readable", 0), "seconds": row["seconds"]}), flush=True)
                return row
            rows = await asyncio.gather(*(measured(team) for team in teams))
            leagues = {}
            for row in rows:
                block = leagues.setdefault(row["league_id"], {"teams": 0, "roster_successes": 0, "news_successes": 0})
                block["teams"] += 1
                block["roster_successes"] += int(row["roster"].get("success", False))
                block["news_successes"] += int(row["news"].get("success", False))
            return {**metadata, "teams": rows, "by_league": leagues,
                    "roster_successes": sum(row["roster"].get("success", False) for row in rows),
                    "news_successes": sum(row["news"].get("success", False) for row in rows),
                    "notice": "Observed public-source readiness only. Chinese model delivery and future availability require separate acceptance."}
        finally:
            # Shielded physical workers retain capacity and may outlive readers;
            # let those workers finish before closing their temporary database.
            coordinator = getattr(store, "_research_requests", None)
            if coordinator and coordinator.draining:
                await asyncio.gather(*list(coordinator.draining), return_exceptions=True)
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--teams", nargs="+")
    parser.add_argument("--timeout", type=int, default=150, choices=range(30, 181))
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(args.json), "teams": len(report["cohort"]),
                      "roster_successes": report.get("roster_successes"), "news_successes": report.get("news_successes")}))


if __name__ == "__main__":
    main()
