"""Reviewed competition seasons, separate from permanent club identities.

The checked-in snapshot supports offline installation. Memberships belong to a
specific starting year, so a promoted club never rewrites a past season. Import
does not fetch the network, migrate a database, or delete historical records.
"""
from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from pathlib import Path


CATALOG_PATH = Path(__file__).resolve().parents[1] / "public/assets/leagues/catalog.json"
CATALOG = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
LEAGUES = CATALOG["leagues"]
SEASONS = CATALOG["seasons"]
MEMBERSHIPS = CATALOG["memberships"]
REVIEWED_AT = CATALOG["reviewed_at"]


def seed_league_catalog(store):
    """Fill absent reviewed records and fields without losing personal data.

    All collections use stable season-scoped IDs. Re-running a catalog upgrade
    preserves older seasons, unknown/custom clubs, and explicit personal blanks.
    """
    with store.atomic() if hasattr(store, "atomic") else nullcontext():
        for collection, records in (("leagues", LEAGUES), ("seasons", SEASONS),
                                    ("league_memberships", MEMBERSHIPS)):
            for defaults in records:
                existing = store.get(collection, defaults["id"])
                if existing is None:
                    store.put(collection, copy.deepcopy(defaults))
                    continue
                missing = {key: copy.deepcopy(value) for key, value in defaults.items() if key not in existing}
                if missing:
                    store.put(collection, {**existing, **missing})
