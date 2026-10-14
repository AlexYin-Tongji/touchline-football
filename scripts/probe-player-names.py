"""Probe: how well does each Chinese-name source cover a real squad?

Read-only. Prints an evidence table; writes nothing. Runs against the local
snapshot so the names and birth dates are the app's own, not a hand-typed list.

    python -I scripts/probe-player-names.py            # all 27, both sources
    python -I scripts/probe-player-names.py --limit 6  # quick look

Both endpoints are already on the reviewed transport's host allowlist, so this
uses the same explicit-proxy path the app uses. The 2.2s global pace the app
enforces is reproduced here, because a probe that ignores rate limits reports
coverage the app could never reach.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.request import getproxies

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from server.player_names import NAME_READINGS, cjk_label, normalize_name, to_simplified  # noqa: E402

DB = ROOT / "data" / "touchline.sqlite3"
SNAPSHOT = "squad-BL1-2026-bayer-leverkusen"
WIKIDATA = "https://www.wikidata.org/w/api.php"
WIKIPEDIA = "https://en.wikipedia.org/w/api.php"
PACE = 2.2
UA = "TouchlineNameProbe/1.0 (local development probe)"


def roster(snapshot):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    row = con.execute("select body from documents where id=?", (snapshot,)).fetchone()
    con.close()
    return json.loads(row[0])["roster"]


def client():
    proxy = getproxies().get("https")
    return httpx.Client(proxy=proxy, trust_env=False, timeout=httpx.Timeout(20),
                        headers={"User-Agent": UA, "Accept-Encoding": "identity"})


def get(cli, url, params):
    for attempt in range(3):
        try:
            response = cli.get(url, params=params)
            if response.status_code == 429:
                time.sleep(8 * (attempt + 1))
                continue
            response.raise_for_status()
            return response.json()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(6 * (attempt + 1))
    return None


def wikipedia_route(cli, name):
    """en title -> zh sitelink title. This is the route that follows the
    Mainland naming convention, which is the one the product wants."""
    payload = get(cli, WIKIPEDIA, {"action": "query", "format": "json", "prop": "langlinks",
                                   "lllang": "zh", "titles": name, "redirects": 1})
    for page in ((payload or {}).get("query", {}).get("pages", {}) or {}).values():
        links = page.get("langlinks")
        if links:
            return links[0]["*"]
    return None


def wikidata_route(cli, name, birth_date):
    """Search entities, keep a footballer whose birth date matches the roster.

    The birth-date comparison is the point: without it the first hit for a
    common name is whoever the search happens to rank first.
    """
    found = get(cli, WIKIDATA, {"action": "wbsearchentities", "format": "json",
                                "search": name, "language": "en", "type": "item", "limit": 8})
    for hit in (found or {}).get("search", []):
        qid = hit.get("id")
        if not qid:
            continue
        time.sleep(PACE)
        entity = get(cli, WIKIDATA, {"action": "wbgetentities", "format": "json", "ids": qid,
                                     "props": "labels|claims|descriptions"})
        item = ((entity or {}).get("entities") or {}).get(qid) or {}
        claims = item.get("claims") or {}
        # P106 must include footballer (Q937857).
        occupations = [c.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
                       for c in claims.get("P106", [])]
        if "Q937857" not in occupations:
            continue
        # P569 birth date, compared as a date string.
        dob = None
        for claim in claims.get("P569", []):
            value = claim.get("mainsnak", {}).get("datavalue", {}).get("value") or {}
            if value.get("time"):
                dob = value["time"][1:11]
                break
        labels = item.get("labels") or {}
        reading = None
        for language in ("zh-hans", "zh"):
            label = labels.get(language)
            if isinstance(label, dict) and cjk_label(label.get("value")):
                reading = (language, label["value"], to_simplified(label["value"]))
                break
        return {"qid": qid, "dob": dob, "dob_matches": dob == birth_date if dob else None,
                "reading": reading}
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default=SNAPSHOT)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    players = roster(args.snapshot)
    if args.limit:
        players = players[: args.limit]
    print(f"{len(players)} players from {args.snapshot}\n")

    stats = {"static": 0, "wikipedia": 0, "wikidata": 0, "wikidata_dob_ok": 0, "none": 0}
    cli = client()
    try:
        for index, player in enumerate(players, 1):
            name, birth = player["name"], player.get("birth_date")
            static = NAME_READINGS.get(normalize_name(name))
            time.sleep(PACE)
            try:
                zh = wikipedia_route(cli, name)
            except Exception as exc:
                zh = f"<error {type(exc).__name__}>"
            time.sleep(PACE)
            try:
                wd = wikidata_route(cli, name, birth)
            except Exception as exc:
                wd = {"error": type(exc).__name__}
            if static:
                stats["static"] += 1
            if zh and not str(zh).startswith("<"):
                stats["wikipedia"] += 1
            if wd and wd.get("reading"):
                stats["wikidata"] += 1
                if wd.get("dob_matches"):
                    stats["wikidata_dob_ok"] += 1
            if not static and not (zh and not str(zh).startswith("<")) and not (wd and wd.get("reading")):
                stats["none"] += 1
            wd_show = "-"
            if wd and wd.get("reading"):
                lang, published, simplified = wd["reading"]
                wd_show = f"{simplified} [{'dob ok' if wd['dob_matches'] else 'dob MISMATCH: ' + str(wd.get('dob'))}]"
            print(f"{index:2}. {name}  ({birth})")
            print(f"    静态表: {static or '-'}")
            print(f"    中文维基: {zh or '-'}")
            print(f"    维基数据: {wd_show}")
    finally:
        cli.close()

    print("\n=== coverage over", len(players), "players ===")
    for key, value in stats.items():
        print(f"  {key:16} {value}")


if __name__ == "__main__":
    main()
