"""Replace club crests with one reviewed source, then normalise every asset.

Two problems drove this. The shipped crests are a mix of sources, so they do not
share a drawing convention, and three of them are browser tab icons rather than
crests: `liverpool.png` is a 180x180 apple-touch-icon whose artwork fills 38% of
its canvas, so the bird renders at about half the size it should. And
`inter.png` carries a decorative alpha channel — its corners are fully opaque —
so a round or rounded mask trims the artwork it appears to be padding.

ESPN publishes a per-club crest at one size, and the catalogue already carries
the provider id for every club, so the replacement is traceable to a single
source rather than assembled by hand.

Nothing is written until the replacement has been fetched and measured. A club
whose candidate is missing, too small, or not actually transparent keeps the
file it has; the run reports it instead of quietly shipping something worse.

Usage:
    python -I scripts/fetch-crest-assets.py            # measure, write nothing
    python -I scripts/fetch-crest-assets.py --write    # replace measured crests
    python -I scripts/fetch-crest-assets.py --clubs liverpool,inter
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover - a maintainer-facing script
    raise SystemExit("需要 Pillow：python -m pip install -r scripts/requirements-dev.txt")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CATALOG = ROOT / "public/assets/clubs/catalog.json"
ATTRIBUTION = ROOT / "public/assets/clubs/attribution.json"
ASSET_DIR = ROOT / "public/assets/clubs"
REVIEWED_AT = time.strftime("%Y-%m-%d")

# `a.espncdn.com` is the same image host the roster headshots already come from,
# and the size segment is part of the address rather than a transform.
CREST_URL = "https://a.espncdn.com/i/teamlogos/soccer/500/{provider_id}.png"
CREST_SOURCE = "https://www.espn.com/soccer/team/_/id/{provider_id}"
MIN_SIDE = 200
# Coverage is reported, not required. Some club crests genuinely are wide
# banners rather than square shields, and normalisation crops each file to its
# own artwork, so a low figure here means "this will be scaled up", not "this
# source is wrong".
ATTRIBUTION_TEXT = "队徽来自 ESPN 球队资料页；商标及图形权利归所属俱乐部，未声明为开放授权。"


def use_utf8_stdout():
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def fetch_bytes(url, timeout=25):
    request = urllib.request.Request(url, headers={"User-Agent": "Touchline/2.0 (personal football reader)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise urllib.error.HTTPError(url, response.status, "unexpected status", response.headers, None)
        return response.read()


def coverage(image):
    """Fraction of the canvas the artwork occupies, by its opaque bounding box."""
    box = image.getchannel("A").point(lambda value: 255 if value > 8 else 0).getbbox()
    if not box:
        return 0.0, None
    width, height = box[2] - box[0], box[3] - box[1]
    return (width * height) / (image.width * image.height), box


def measure(payload):
    """Whether this candidate is a usable crest, and why not when it is not.

    A palette image is accepted: it carries real transparency, it just stores
    its colours as indices. `normalize-crests.py` converts it on the way in.
    What is refused is an image with no transparency at all, which cannot sit on
    a rounded tile without its own background showing as a square.
    """
    image = Image.open(io.BytesIO(payload))
    image.load()
    if image.format != "PNG":
        return None, f"格式为 {image.format}，需要 PNG"
    if min(image.width, image.height) < MIN_SIDE:
        return None, f"尺寸只有 {image.width}x{image.height}"
    if "A" not in image.getbands() and "transparency" not in image.info:
        return None, "没有透明通道，拼在圆角底板上会露出方角"
    alpha = image.convert("RGBA").getchannel("A")
    corners = [alpha.getpixel(point) for point in
               ((0, 0), (image.width - 1, 0), (0, image.height - 1), (image.width - 1, image.height - 1))]
    if any(value > 16 for value in corners):
        return None, f"四角不透明 {corners}，没有真正的透明背景"
    filled, box = coverage(image.convert("RGBA"))
    return {"size": f"{image.width}x{image.height}", "coverage": round(filled, 3),
            "box": list(box), "corners": corners}, None


def main(args):
    use_utf8_stdout()
    clubs = json.loads(CATALOG.read_text(encoding="utf-8"))
    notes = json.loads(ATTRIBUTION.read_text(encoding="utf-8"))
    credits = {row["id"]: row for row in notes.get("clubs", [])}
    compatibility = notes.setdefault("compatibility_assets", [])
    registered = {row.get("crest_url") for row in compatibility}
    selected = args.clubs.split(",") if args.clubs else [club["id"] for club in clubs]
    known = {club["id"] for club in clubs}
    unknown = [name for name in selected if name not in known]
    if unknown:
        raise SystemExit("unknown club ids: " + ", ".join(unknown))
    # Compatibility entries pin *older* addresses such as
    # `/assets/clubs/real-madrid.svg`, whose bytes a separate test hashes and
    # which saved profiles may still hold. Those files are not touched here: the
    # replacement writes `<id>.png`, and the entry stays registered so the orphan
    # check still accounts for the older file on disk.

    replaced, failed = [], []
    for club in clubs:
        if club["id"] not in selected:
            continue
        provider_id = (club.get("provider_ids") or {}).get("espn")
        if not provider_id:
            failed.append((club["id"], "目录中没有 ESPN 球队编号"))
            print(f"  {club['id']:26s} 保留原文件：目录中没有 ESPN 球队编号", flush=True)
            continue
        url = CREST_URL.format(provider_id=provider_id)
        try:
            payload = fetch_bytes(url)
            result, why = measure(payload)
        except Exception as exc:
            result, why = None, f"{type(exc).__name__}: {exc}"
        if result is None:
            failed.append((club["id"], why))
            print(f"  {club['id']:26s} 保留原文件：{why}", flush=True)
            continue
        print(f"  {club['id']:26s} {result['size']:>9s}  内容占比 {result['coverage'] * 100:5.1f}%"
              f"  {'写入' if args.write else '可用'}", flush=True)
        replaced.append((club, payload, result, provider_id))

    print(f"\n可替换 {len(replaced)} 个，保留原文件 {len(failed)} 个。", flush=True)
    if not args.write:
        print("加 --write 才会写入文件与档案。", flush=True)
        return 0

    for club, payload, result, provider_id in replaced:
        target = ASSET_DIR / f"{club['id']}.png"
        old_url = club["crest_url"]
        old_body = (ROOT / "public" / old_url.lstrip("/")).read_bytes() if old_url.startswith("/assets/") else b""
        target.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()

        row = credits.setdefault(club["id"], {})
        row.update(id=club["id"], english_name=club["english_name"],
                   crest_url=f"/assets/clubs/{club['id']}.png",
                   crest_source_url=CREST_SOURCE.format(provider_id=provider_id),
                   crest_asset_url=CREST_URL.format(provider_id=provider_id),
                   crest_attribution=ATTRIBUTION_TEXT, reviewed_at=REVIEWED_AT,
                   sha256=digest, bytes=len(payload))
        if old_url != row["crest_url"] and old_body:
            # The replaced file stays on disk, so it has to be accounted for:
            # the catalogue no longer points at it and it must not read as an
            # orphan. It is also the address a saved profile may still hold.
            if old_url not in registered:
                compatibility.append({
                    "id": club["id"], "crest_url": old_url,
                    "crest_asset_url": club.get("crest_asset_url", ""),
                    "crest_source_url": club.get("crest_source_url", ""),
                    "sha256": hashlib.sha256(old_body).hexdigest(), "bytes": len(old_body),
                    "reviewed_at": REVIEWED_AT,
                    "purpose": "旧版目录使用过的地址；保留原文件以便已保存的球队档案继续显示。",
                    "crest_attribution": club.get("crest_attribution", "")})
                registered.add(old_url)
        club["crest_url"] = row["crest_url"]
        club["crest_source_url"] = row["crest_source_url"]
        club["crest_asset_url"] = row["crest_asset_url"]
        club["crest_attribution"] = ATTRIBUTION_TEXT
        # Normalisation rewrites this file later, so record what it replaced.
        club.setdefault("crest_history", []).append({"replaced_asset_url": old_url, "replaced_at": REVIEWED_AT})

    CATALOG.write_text(json.dumps(clubs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ATTRIBUTION.write_text(json.dumps(notes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {len(replaced)} 个队徽，并更新 catalog.json 与 attribution.json。", flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="replace the measured crests")
    parser.add_argument("--clubs", help="comma-separated club ids")
    raise SystemExit(main(parser.parse_args()))
