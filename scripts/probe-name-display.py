"""Probe: does a checked reading actually reach the roster the UI reads?

Read-only apart from a temporary store. The resolver test in
``scripts/probe-team-names.py`` shows what the sources say; this shows that the
answer survives storage and the display path, which is a different failure mode
— the first version of this work produced correct readings that the interface
never saw, because the assembly point only consulted the static table.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import team_center, team_names  # noqa: E402
from server.storage import Store  # noqa: E402

DB = ROOT / "data" / "touchline.sqlite3"


def snapshot():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    row = con.execute("select body from documents where id=?",
                      ("squad-BL1-2026-bayer-leverkusen",)).fetchone()
    con.close()
    return json.loads(row[0])


async def main():
    snap = snapshot()
    team_id = "bayer-leverkusen"
    players = snap["roster"]
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "probe.sqlite3")
        try:
            # Check a handful the way the names pass would.
            for player in players[:4]:
                record, reason = await team_names.resolve_reading(store, player, team_id)
                if record:
                    team_names.store_reading(store, player, team_id, record)
            # Now read them back the way the UI does.
            print("via team_names_for (the display path):")
            for player in players[:6]:
                read = team_center.team_names_for(store, player, team_id)
                print(f"  {player['name']:22} -> {(read or {}).get('name_zh') or '-'} "
                      f"({(read or {}).get('name_zh_source') or 'none'})")
            print()
            print("via with_reading on a stored row (what the roster renders):")
            from server.player_names import with_reading
            for player in players[:6]:
                row = with_reading(dict(player), source_reading=team_center.team_names_for(store, player, team_id),
                                   team_id=team_id)
                print(f"  {row['name']:22} -> {row.get('name_zh') or '(源拼写)'} "
                      f"{row.get('name_zh_source_url') or ''}")
        finally:
            store.close()


if __name__ == "__main__":
    asyncio.run(main())
