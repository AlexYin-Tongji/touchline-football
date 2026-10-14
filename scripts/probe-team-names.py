"""Probe: does the real resolver produce citable readings for a real squad?

Read-only against the local snapshot and the reviewed public endpoints. Writes
nothing. This is the end-to-end check the unit tests cannot make: it exercises
the actual search, the identity comparisons and the two name routes against
living sources.

    python -I scripts/probe-team-names.py --limit 4
    python -I scripts/probe-team-names.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import team_names  # noqa: E402
from server.storage import Store  # noqa: E402

DB = ROOT / "data" / "touchline.sqlite3"
SNAPSHOT = "squad-BL1-2026-bayer-leverkusen"


def roster(snapshot):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    row = con.execute("select body from documents where id=?", (snapshot,)).fetchone()
    con.close()
    return json.loads(row[0])["roster"]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default=SNAPSHOT)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    players = roster(args.snapshot)
    if args.limit:
        players = players[: args.limit]

    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "probe.sqlite3")
        try:
            obtained = refused = 0
            for index, player in enumerate(players, 1):
                record, reason = await team_names.resolve_reading(store, player, "bayer-leverkusen")
                if record:
                    obtained += 1
                    proof = record["name_zh_identity"]
                    print(f"{index:2}. {player['name']:22} -> {record['name_zh']:20} "
                          f"[{record['name_zh_source']}] {proof['qid']}")
                else:
                    refused += 1
                    print(f"{index:2}. {player['name']:22} -> (未取得: {reason})")
            print(f"\n=== {obtained} obtained, {refused} refused, of {len(players)} ===")
        finally:
            store.close()


if __name__ == "__main__":
    asyncio.run(main())
