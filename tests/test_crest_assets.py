"""Crest assets: one source, one canvas, and a real transparent background.

Three separate defects this pins down. `liverpool.png` was a 180x180 browser
tab icon whose bird filled 38% of its canvas, so it rendered at about half the
size of every other crest. `inter.png` carried a decorative alpha channel —
fully opaque corners — so a rounded tile trimmed artwork it looked like it was
padding. And the shipped set drew from a dozen sources with no shared
convention, so no single size or margin was right for all of them.

Reads the files with `struct` and `zlib` rather than an image library: the test
must run wherever the app runs, and image processing is a maintainer-only
dependency.
"""
import functools
import json
import struct
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "public/assets/clubs"
CATALOG = ASSET_DIR / "catalog.json"
ATTRIBUTION = ASSET_DIR / "attribution.json"
# The frame every crest is normalised into, and the artwork box inside it.
# `normalize-crests.py` writes these; the tests read them back from pixel data.
CANVAS = 256
CONTENT = 232
CORNER_TOLERANCE = 16


@functools.lru_cache(maxsize=None)
def png_alpha(path_text):
    """Size, corner alpha values and artwork coverage, without any dependency.

    Cached because four assertions read the same 96 files, and unfiltering a
    PNG byte by byte is by far the expensive part. The files do not change
    while a test run is in progress, so the key is just the path.
    """
    path = Path(path_text)
    body = path.read_bytes()
    if body[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"{path.name} 不是 PNG")
    width, height, depth, colour = struct.unpack(">IIBB", body[16:26])
    if colour not in {4, 6}:
        raise AssertionError(f"{path.name} 没有透明通道（颜色类型 {colour}）")
    channels = 4 if colour == 6 else 2
    if depth != 8:
        raise AssertionError(f"{path.name} 位深为 {depth}，测试只处理 8 位")
    position, chunks = 8, []
    while position < len(body):
        length = struct.unpack(">I", body[position:position + 4])[0]
        kind = body[position + 4:position + 8]
        if kind == b"IDAT":
            chunks.append(body[position + 8:position + 8 + length])
        elif kind == b"IEND":
            break
        position += 12 + length
    raw, stride = zlib.decompress(b"".join(chunks)), width * channels
    alpha_index = channels - 1
    previous = bytes(stride)
    row_alpha, xs, ys = [], [], []
    cursor = 0
    for y in range(height):
        filter_type = raw[cursor]
        cursor += 1
        line = bytearray(raw[cursor:cursor + stride])
        cursor += stride
        if filter_type == 1:
            for index in range(channels, stride):
                line[index] = (line[index] + line[index - channels]) & 255
        elif filter_type == 2:
            for index in range(stride):
                line[index] = (line[index] + previous[index]) & 255
        elif filter_type == 3:
            for index in range(stride):
                left = line[index - channels] if index >= channels else 0
                line[index] = (line[index] + ((left + previous[index]) >> 1)) & 255
        elif filter_type == 4:
            for index in range(stride):
                left = line[index - channels] if index >= channels else 0
                up = previous[index]
                diagonal = previous[index - channels] if index >= channels else 0
                estimate = left + up - diagonal
                distances = (abs(estimate - left), abs(estimate - up), abs(estimate - diagonal))
                predictor = left if distances[0] <= distances[1] and distances[0] <= distances[2] else (up if distances[1] <= distances[2] else diagonal)
                line[index] = (line[index] + predictor) & 255
        row_alpha.append(bytes(line[alpha_index::channels]))
        for x, value in enumerate(line[alpha_index::channels]):
            if value > 8:
                xs.append(x)
                ys.append(y)
        previous = bytes(line)
    if not xs:
        raise AssertionError(f"{path.name} 全透明")
    box = (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)
    corners = [row_alpha[0][0], row_alpha[0][width - 1], row_alpha[height - 1][0], row_alpha[height - 1][width - 1]]
    return {"size": (width, height), "corners": corners,
            "coverage": (box[0] * box[1]) / (width * height), "art": box}


class CrestAssetTests(unittest.TestCase):
    def setUp(self):
        self.clubs = json.loads(CATALOG.read_text(encoding="utf-8"))
        self.credits = {row["id"]: row for row in
                        json.loads(ATTRIBUTION.read_text(encoding="utf-8"))["clubs"]}

    def test_every_crest_is_a_square_png_from_the_same_source(self):
        for club in self.clubs:
            with self.subTest(club=club["id"]):
                self.assertTrue(club["crest_url"].endswith(".png"), club["crest_url"])
                self.assertIn("a.espncdn.com", club["crest_asset_url"])
                facts = png_alpha(str(ROOT / "public" / club["crest_url"].lstrip("/")))
                self.assertEqual(facts["size"], (CANVAS, CANVAS), "画布应为统一的正方形")

    def test_every_crest_artwork_fills_the_frame_it_was_given(self):
        # Coverage of a *square* canvas cannot be the test: Union Berlin's crest
        # is a wide banner and Tottenham's is a tall shield, so neither can
        # occupy much of a square. What must hold is that the artwork reaches
        # the frame on its long axis, which is what makes every crest render at
        # the same visual size regardless of the margin its source shipped.
        for club in self.clubs:
            with self.subTest(club=club["id"]):
                facts = png_alpha(str(ROOT / "public" / club["crest_url"].lstrip("/")))
                self.assertEqual(max(facts["art"]), CONTENT,
                                 f"{club['id']} 的图形没有填满取景框：{facts['art']}")
                self.assertEqual(facts["size"], (CONTENT + (CANVAS - CONTENT), CANVAS))

    def test_no_crest_keeps_an_unremoved_background(self):
        # The user's own words were "队徽最好是透明的". An unremoved background
        # is what the corner check detects: if the four corners are opaque, the
        # picture is a rectangle sitting on its own backdrop, and a rounded tile
        # trims artwork it appeared to be padding — which is what `inter.png`
        # did with fully opaque corners behind a decorative alpha channel.
        #
        # A single flat colour is deliberately *not* a failure: Juventus' crest
        # is drawn in black alone, and a "too few colours" rule would reject a
        # real crest.
        for club in self.clubs:
            with self.subTest(club=club["id"]):
                facts = png_alpha(str(ROOT / "public" / club["crest_url"].lstrip("/")))
                self.assertTrue(all(value <= CORNER_TOLERANCE for value in facts["corners"]),
                                f"{club['id']} 的四角不透明：{facts['corners']}")

    def test_the_attribution_entry_matches_the_file_it_describes(self):
        import hashlib
        for club in self.clubs:
            with self.subTest(club=club["id"]):
                body = (ROOT / "public" / club["crest_url"].lstrip("/")).read_bytes()
                row = self.credits[club["id"]]
                self.assertEqual(hashlib.sha256(body).hexdigest(), row["sha256"])
                self.assertEqual(len(body), row["bytes"])
                self.assertIn("权利归", row["crest_attribution"])

    def test_a_replaced_crest_leaves_its_predecessor_accounted_for(self):
        # The old file stays on disk for saved profiles, so it has to sit in
        # either the catalogue or the compatibility list, never nowhere.
        manifest = json.loads(ATTRIBUTION.read_text(encoding="utf-8"))
        used = {club["crest_url"] for club in self.clubs}
        registered = {row["crest_url"] for row in manifest.get("compatibility_assets", [])}
        extensions = {".png", ".svg", ".jpg", ".jpeg", ".webp", ".avif"}
        on_disk = {"/assets/clubs/" + path.name for path in ASSET_DIR.iterdir()
                   if path.is_file() and path.suffix.lower() in extensions}
        self.assertEqual(on_disk - used - registered, set())


if __name__ == "__main__":
    unittest.main()
