"""Rebuild the derived search index, optionally backfilling article bodies.

The index holds token frequencies and character ranges, never sentences. Two
different jobs live here because they have different costs:

* ``--rebuild`` drops every index row and rebuilds it from stored records. For
  articles whose body was never retained this produces a **title-only** record,
  marked ``body_owed`` so the next real read replaces it.
* ``--backfill`` re-reads each article still marked ``body_owed`` from its
  original URL, which is what gives an old article body terms. It is network
  work — hundreds of pages, subject to each publisher's rate limiting and to the
  transport coordinator's 2-per-host limit — so it is never implicit.

Nothing here writes an article body to disk. A backfilled body is indexed and
then dropped exactly as a freshly collected one is.

    .\\.venv\\Scripts\\python.exe scripts/rebuild-search-index.py --rebuild
    .\\.venv\\Scripts\\python.exe scripts/rebuild-search-index.py --backfill --limit 50
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import research, search_index  # noqa: E402
from server.storage import Store  # noqa: E402


def rebuild(store):
    for row in store.list("search_index"):
        store.delete("search_index", row["id"])
    return search_index.rebuild(store)


async def backfill(store, limit, delay):
    owed = [row for row in store.list("search_index") if row.get("body_owed")]
    if limit:
        owed = owed[:limit]
    read, failed = 0, []
    for row in owed:
        item = store.get("evidence", row["id"]) or {}
        if item.get("origin") not in {"web", "rss"} or not item.get("url"):
            continue
        try:
            await research.read_evidence_source(store, row["id"])
            read += 1
        except Exception as exc:
            failed.append({"evidence_id": row["id"], "reason": type(exc).__name__})
        # Publishers are shared between teams; a tight loop looks like a crawler.
        await asyncio.sleep(delay)
    remaining = len([row for row in store.list("search_index") if row.get("body_owed")])
    return {"attempted": len(owed), "read": read, "failed": len(failed),
            "still_owed": remaining, "failures": failed[:20]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(ROOT / "data" / "touchline.sqlite3"))
    parser.add_argument("--rebuild", action="store_true", help="drop and rebuild every index row")
    parser.add_argument("--backfill", action="store_true", help="re-read articles that still owe a body")
    parser.add_argument("--limit", type=int, default=0, help="cap how many articles to backfill this run")
    parser.add_argument("--delay", type=float, default=0.8, help="seconds between article reads")
    args = parser.parse_args()
    if not (args.rebuild or args.backfill):
        parser.error("choose --rebuild and/or --backfill")

    store = Store(args.database)
    started = time.monotonic()
    if args.rebuild:
        print("rebuild:", rebuild(store))
    if args.backfill:
        print("backfill:", asyncio.run(backfill(store, args.limit, args.delay)))
    print("index rows:", store.count("search_index"),
          "elapsed seconds:", round(time.monotonic() - started, 1))


if __name__ == "__main__":
    main()
