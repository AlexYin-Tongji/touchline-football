"""Reset the local database to a factory-fresh state, keeping only what cannot be rebuilt.

Everything this drops is either a cache, a run journal, or derived content that
the next collection pass regenerates. What it keeps is the set of things that
exist *only* here:

  * ``secrets``               — encrypted provider keys and the LLM key.
  * ``model_profiles``        — the two model profiles the user configured.
  * ``active_model``          — which profile is selected.
  * ``model_roles``           — per-role model assignment.
  * ``settings``              — followed teams, spoiler setting, depth, network.

The four sentinel keys (``model_profiles_migrated``, ``personal_v7_defaults``,
``transient_bodies_v1``, ``legacy_source_provenance_v1``) are kept for a
different reason: they tell the storage layer its migrations already ran. Drop
``transient_bodies_v1`` and opening the store writes a ``before-migration``
backup and re-runs ``_migrate_external_bodies`` over an already-migrated
database. ``demo_seed_v1`` is deliberately *not* kept, so the clearly-fictional
demo rows are re-seeded on the next open.

The key file is never touched — ``data/touchline.key`` and the database are one
unit, and deleting the key makes every stored secret unrecoverable.

Run with the project venv::

    .\\.venv\\Scripts\\python.exe scripts/reset-data.py --dry-run
    .\\.venv\\Scripts\\python.exe scripts/reset-data.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data" / "touchline.sqlite3"

# Documents in these collections survive: they are configuration, not content.
KEEP_COLLECTIONS = frozenset({"model_profiles"})

# Metadata keys that survive. Everything else in `metadata` is dropped.
KEEP_METADATA = frozenset({
    "active_model",
    "model_roles",
    "settings",
    # Migration sentinels. See the module docstring for why each is load-bearing.
    "model_profiles_migrated",
    "personal_v7_defaults",
    "transient_bodies_v1",
    "legacy_source_provenance_v1",
})

# Reseeded unconditionally at startup by `seed()`, so clearing them is harmless.
# Listed for the report only; they are cleared along with everything else.
RESEEDED = frozenset({
    "teams", "leagues", "seasons", "league_memberships", "league_matches", "standings",
})


def survey(con: sqlite3.Connection) -> dict:
    collections = dict(con.execute(
        "SELECT collection, COUNT(*) FROM documents GROUP BY collection ORDER BY 2 DESC").fetchall())
    metadata = [row[0] for row in con.execute("SELECT key FROM metadata").fetchall()]
    secrets = [row[0] for row in con.execute("SELECT key FROM secrets").fetchall()]
    return {"collections": collections, "metadata": metadata, "secrets": secrets}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Report what would be dropped; change nothing.")
    parser.add_argument("--database", type=Path, default=DATABASE)
    args = parser.parse_args()

    if not args.database.is_file():
        print(f"no database at {args.database}")
        return 2
    key_file = args.database.with_suffix(".key")
    if not key_file.is_file():
        print(f"refusing to touch {args.database.name}: its key file {key_file.name} is missing, "
              "so stored secrets could not be read back")
        return 2

    con = sqlite3.connect(f"file:{args.database}?mode={'ro' if args.dry_run else 'rw'}", uri=True)
    con.row_factory = sqlite3.Row
    try:
        report = survey(con)
        print(f"database : {args.database}")
        print(f"key file : {key_file} (left untouched)")
        print(f"secrets  : {len(report['secrets'])} kept")
        print()

        keep_docs = {name: count for name, count in report["collections"].items() if name in KEEP_COLLECTIONS}
        drop_docs = {name: count for name, count in report["collections"].items() if name not in KEEP_COLLECTIONS}
        keep_meta = sorted(key for key in report["metadata"] if key in KEEP_METADATA)
        drop_meta = sorted(key for key in report["metadata"] if key not in KEEP_METADATA)

        print(f"documents: {sum(keep_docs.values())} rows kept across {len(keep_docs)} collections")
        for name, count in sorted(keep_docs.items()):
            print(f"    keep  {name:24} {count}")
        print(f"documents: {sum(drop_docs.values())} rows in {len(drop_docs)} collections would be dropped")
        for name, count in sorted(drop_docs.items(), key=lambda row: -row[1]):
            note = "  (reseeded at startup)" if name in RESEEDED else ""
            print(f"    drop  {name:24} {count}{note}")
        print()
        print(f"metadata : {len(keep_meta)} keys kept, {len(drop_meta)} dropped")
        for key in keep_meta:
            print(f"    keep  {key}")
        print(f"    drop  {len(drop_meta)} keys, including: "
              f"{', '.join(drop_meta[:6])}{' …' if len(drop_meta) > 6 else ''}")

        if args.dry_run:
            print("\ndry run: nothing changed. Re-run without --dry-run to apply.")
            return 0

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        survivors = {key: con.execute("SELECT body FROM metadata WHERE key = ?", (key,)).fetchone()
                     for key in KEEP_METADATA}
        survivors = {key: row[0] for key, row in survivors.items() if row is not None}

        with con:
            for name in drop_docs:
                con.execute("DELETE FROM documents WHERE collection = ?", (name,))
            con.execute("DELETE FROM metadata")
            con.executemany("INSERT INTO metadata(key, body) VALUES(?, ?)", sorted(survivors.items()))
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("VACUUM")

        after = survey(con)
        print(f"\nreset complete at {stamp}")
        print(f"  documents: {sum(after['collections'].values())} rows in "
              f"{len(after['collections'])} collections "
              f"{dict(sorted(after['collections'].items()))}")
        print(f"  metadata : {len(after['metadata'])} keys {sorted(after['metadata'])}")
        print(f"  secrets  : {len(after['secrets'])} keys {sorted(after['secrets'])}")
        print(json.dumps({"dropped": drop_docs, "dropped_metadata": len(drop_meta),
                          "kept_metadata": sorted(after["metadata"])}, ensure_ascii=False, indent=2))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
