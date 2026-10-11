"""One canvas for every crest, so each one renders at the same visual size.

Crest sources disagree about how much empty margin belongs to a logo. Liverpool
arrived as a tab icon whose bird occupied 38% of its canvas; Union Berlin's is a
wide banner at 30%; most occupy 65-90%. Because the app scales the whole canvas
to fit, that margin is what decides how large the crest looks — which is why one
club's badge appeared half the size of its neighbours'.

This measures each file's artwork, crops to it, and scales the result into a
fixed frame. Nothing is invented: the crop is the alpha bounding box and the
scale is uniform, so the bytes change but the picture does not.

Usage:
    python -I scripts/normalize-crests.py            # measure, write nothing
    python -I scripts/normalize-crests.py --write    # rewrite the assets
    python -I scripts/normalize-crests.py --clubs arsenal
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover - a maintainer-facing script
    raise SystemExit("需要 Pillow：python -m pip install -r scripts/requirements-dev.txt")

ROOT = Path(__file__).resolve().parents[1]

ASSET_DIR = ROOT / "public/assets/clubs"
CATALOG = ASSET_DIR / "catalog.json"
ATTRIBUTION = ASSET_DIR / "attribution.json"
REVIEWED_AT = time.strftime("%Y-%m-%d")
# One square canvas with a small even margin. The result is that a crest's
# artwork fills the frame regardless of how much empty space its source had.
CANVAS = 256
CONTENT = 232
ALPHA_FLOOR = 8
# A crest that arrived at a tiny size is enlarged only this far; beyond it the
# result is invented detail rather than a larger picture.
MAX_UPSCALE = 6.0


def use_utf8_stdout():
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def trim(image):
    """The artwork's own box, by alpha. Palette images are converted first."""
    rgba = image.convert("RGBA")
    mask = rgba.getchannel("A").point(lambda value: 255 if value > ALPHA_FLOOR else 0)
    box = mask.getbbox()
    return (rgba, box) if box else (rgba, None)


def normalized(image):
    """Crop to the artwork, scale it into the frame, centre it.

    Scaling is uniform and applies in both directions: a crest that arrived
    with a wide margin is enlarged to the frame, and one that filled its canvas
    is reduced to leave the margin every other crest has. Upscaling is capped so
    a genuinely low-resolution asset is not blown up into a blur.
    """
    rgba, box = trim(image)
    if box is None:
        raise ValueError("整张图都是透明的")
    art = rgba.crop(box)
    scale = min(CONTENT / art.width, CONTENT / art.height)
    scale = min(scale, MAX_UPSCALE) if scale > 1 else scale
    target = (max(1, round(art.width * scale)), max(1, round(art.height * scale)))
    if target != art.size:
        art = art.resize(target, Image.LANCZOS)
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    canvas.paste(art, ((CANVAS - art.width) // 2, (CANVAS - art.height) // 2), art)
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), {"canvas": CANVAS, "content": list(art.size),
                               "coverage": round(art.width * art.height / (CANVAS * CANVAS), 3)}


def main(args):
    use_utf8_stdout()
    clubs = json.loads(CATALOG.read_text(encoding="utf-8"))
    notes = json.loads(ATTRIBUTION.read_text(encoding="utf-8"))
    credits = {row["id"]: row for row in notes.get("clubs", [])}
    selected = args.clubs.split(",") if args.clubs else [club["id"] for club in clubs]
    known = {club["id"] for club in clubs}
    unknown = [name for name in selected if name not in known]
    if unknown:
        raise SystemExit("unknown club ids: " + ", ".join(unknown))

    planned, failed = [], []
    for club in clubs:
        if club["id"] not in selected:
            continue
        path = ROOT / "public" / club["crest_url"].lstrip("/")
        if path.suffix.lower() != ".png":
            # A vector crest has no raster margin to normalise, and rewriting it
            # through a bitmap would trade a vector for a picture of one.
            print(f"  {club['id']:26s} 跳过：{path.suffix} 不是位图", flush=True)
            continue
        before = path.read_bytes()
        try:
            after, facts = normalized(Image.open(io.BytesIO(before)))
        except Exception as exc:
            failed.append((club["id"], f"{type(exc).__name__}: {exc}"))
            print(f"  {club['id']:26s} 保留原文件：{exc}", flush=True)
            continue
        print(f"  {club['id']:26s} {facts['content'][0]:>3d}x{facts['content'][1]:<3d}"
              f" 占画布 {facts['coverage'] * 100:5.1f}%  {len(before):>7d} -> {len(after):>6d} B", flush=True)
        planned.append((club, path, before, after, facts))

    print(f"\n可规范化 {len(planned)} 个，失败 {len(failed)} 个。", flush=True)
    if not args.write:
        print("加 --write 才会写入文件与档案。", flush=True)
        return 0

    for club, path, before, after, facts in planned:
        path.write_bytes(after)
        row = credits.setdefault(club["id"], {})
        # The hash is the point of the entry, so it is updated in the same step
        # as the file. The value it replaced is kept so the change is auditable.
        row.update(sha256=hashlib.sha256(after).hexdigest(), bytes=len(after), reviewed_at=REVIEWED_AT,
                   normalized={"canvas": facts["canvas"], "content": facts["content"],
                               "coverage": facts["coverage"], "at": REVIEWED_AT,
                               "source_sha256": hashlib.sha256(before).hexdigest()})
    ATTRIBUTION.write_text(json.dumps(notes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已规范化 {len(planned)} 个队徽，并更新 attribution.json。", flush=True)
    print("若目录里还有旧的 SVG/JPG 文件，它们已被登记为兼容资产，不需要处理。", flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the crest assets")
    parser.add_argument("--clubs", help="comma-separated club ids")
    raise SystemExit(main(parser.parse_args()))
