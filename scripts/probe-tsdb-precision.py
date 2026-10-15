"""End-to-end check of the community portrait path against real data.

Runs the product's own resolver — not a copy of it — over a real squad and a
real publisher, with a throwaway store in a temp directory. Nothing here touches
the user's data or the shipped cache, and no model is called.

    .\\.venv\\Scripts\\python.exe -I scripts/probe-tsdb-precision.py
    .\\.venv\\Scripts\\python.exe -I scripts/probe-tsdb-precision.py --team 133738 esp.1 86
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.storage import Store  # noqa: E402

ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/teams/{team}/roster"
# A squad source row is built the way the product builds it, so the resolver is
# exercised through its real inputs rather than a fixture shaped to pass.
COUNTRIES = {"England": "英格兰", "Spain": "西班牙", "Brazil": "巴西",
             "France": "法国", "Germany": "德国", "Portugal": "葡萄牙"}


def folded(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join("".join(char if char.isalnum() else " " for char in text).split())


def flatten(items):
    out = []
    for item in items:
        if item.get("items"):
            out += flatten(item["items"])
        elif item.get("id"):
            out.append(item)
    return out


def fetch_roster(league: str, team: str):
    import urllib.request
    request = urllib.request.Request(ESPN.format(league=league, team=team),
                                     headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    rows = []
    for athlete in flatten(payload.get("athletes") or []):
        name = athlete.get("displayName") or ""
        born = str(athlete.get("dateOfBirth") or "")[:10]
        if not name or not born:
            continue
        citizenship = athlete.get("citizenship") or ""
        rows.append({"id": "espn-player-" + str(athlete["id"]),
                     "provider_id": str(athlete["id"]), "name": name,
                     "birth_date": born, "number": str(athlete.get("jersey") or ""),
                     "nationality": COUNTRIES.get(citizenship, citizenship),
                     "position_group": (athlete.get("position") or {}).get("abbreviation") or "OTHER"})
    return rows


async def main() -> int:
    parser = argparse.ArgumentParser(description="用真实数据验收社区照片路径。")
    parser.add_argument("--team", default="359", help="ESPN 球队编号")
    parser.add_argument("--league", default="eng.1", help="ESPN 联赛代号")
    parser.add_argument("--limit", type=int, default=29, help="最多核对多少名球员")
    args = parser.parse_args()

    from server.community_portraits import resolve_portrait

    rows = fetch_roster(args.league, args.team)[:args.limit]
    print(f"真实阵容 {len(rows)} 人（ESPN {args.league}/{args.team}）")
    print("用产品自身的 resolve_portrait 逐人核对，临时库，不写用户数据。\n")

    with tempfile.TemporaryDirectory(prefix="touchline-probe-") as folder:
        store = Store(str(Path(folder) / "probe.db"))
        try:
            stats = {"completed": 0, "missing": 0, "ambiguous": 0, "unavailable": 0}
            matches, gaps = [], []
            for row in rows:
                result = await resolve_portrait(store, row, args.team)
                status = result.get("status")
                stats[status] = stats.get(status, 0) + 1
                if status == "completed":
                    matches.append((row["name"], result.get("photo_match"), row["birth_date"]))
                else:
                    gaps.append((row["name"], result.get("reason_code"),
                                 result.get("namesake_blocked", False)))
        finally:
            # SQLite holds the file open on Windows, so the temp directory cannot
            # be removed until the Store is closed. The report is printed below,
            # after the connection is gone.
            store.close()

    print("== 结果 ==")
    for key in ("completed", "missing", "ambiguous", "unavailable"):
        print(f"  {key:12} {stats.get(key, 0)}")
    total = len(rows)
    print(f"\n  覆盖率 {stats['completed']}/{total} = {stats['completed'] / max(1, total) * 100:.0f}%")

    print("\n== 挂上的照片（逐条核对用）==")
    for name, match, born in matches:
        print(f"  {name:30} {born}  {match}")
    print("\n== 未挂图的原因 ==")
    for name, code, blocked in gaps:
        print(f"  {name:30} {code}{'  (同名遮挡)' if blocked else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
