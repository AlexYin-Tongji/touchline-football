"""The photo domain of the shared review loop: pictures code already proved.

``review_loop`` owns the decision — what to retry, what is ambiguous, what needs a
person. This module owns the part that is specific to photos: which rows may be
looked at, how the bytes are fetched, and what a verdict is allowed to mean.

Whoever the picture belongs to has already been decided by a source. The review
here cannot add a photo, a licence, an address or an identity claim to a row —
nothing in this module writes a player field.

The bytes are untrusted input. They are fetched through the same pinned public
address transport as every other source read, with a size cap, a content-type
allowlist and a magic-number check, because a "photo" a source serves is
attacker-controlled data like any other page.
"""
from __future__ import annotations

import asyncio

from . import review_loop
from .ingestion import IngestionError, fetch_document
from .provider import image_block

# Small enough that one pass stays cheap and the request stays under the
# provider's image limits; large enough that a squad's worth of verdicts is
# useful when it comes back.
MAX_REVIEW_BATCH = review_loop.MAX_ITEMS
# A portrait, not a page: 4 MiB is generous for a headshot and still bounded.
MAX_IMAGE_BYTES = 4 * 1024 * 1024
# Magic numbers, because the Content-Type header is the source's claim and this
# is the file's. A mismatch is refused rather than forwarded to the model.
_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"),
          (b"RIFF", "image/webp"))

ACTION_KEEP = review_loop.ACTION_KEEP
ACTION_RETRY = review_loop.ACTION_RETRY
ACTION_INVESTIGATE = review_loop.ACTION_INVESTIGATE


def media_type_of(data):
    """The image type the file actually is, or ``""`` when it is not one.

    A source's ``Content-Type`` is a claim; these bytes are the answer. Format
    sniffing here also keeps an HTML error page from being base64'd into a paid
    request as if it were a portrait.
    """
    if not isinstance(data, bytes) or not data:
        return ""
    for prefix, kind in _MAGIC:
        if data.startswith(prefix):
            if kind == "image/webp" and data[8:12] != b"WEBP":
                continue
            return kind
    return ""


def reviewable(players):
    """Rows that already carry a source-backed photo, in stable order.

    A row with a picture but no source URL is excluded on purpose: the review
    exists to judge a *sourced* image, and admitting an unsourced one would let
    the model's answer stand in for the provenance check it never made.
    """
    chosen = []
    for player in players or []:
        url = str(player.get("photo_url") or "")
        source = str(player.get("photo_source_url") or player.get("photo_file_page") or "")
        if url.startswith("https://") and source.startswith("http") and player.get("photo_match"):
            chosen.append(player)
    return chosen


async def _download(url, *, allowed_hosts=None):
    """Fetch one portrait through the shared pinned-address transport.

    ``fetch_document`` is blocking and resolves a pinned public address per hop;
    it runs in a worker thread so one slow source cannot stall the event loop the
    rest of the refresh shares.
    """
    call = lambda: fetch_document(url, allowed_hosts=allowed_hosts, max_body=MAX_IMAGE_BYTES,  # noqa: E731
                                 content_types=("image/",))
    return await asyncio.get_running_loop().run_in_executor(None, call)


async def build_board(players, acceptance=None, *, limit=MAX_REVIEW_BATCH, allowed_hosts=None):
    """The items a photo review may see, and the pictures that go with them.

    Returns ``(items, blocks, notes)``. Items with an ``image_index`` have a
    picture at that position in ``blocks``; items without one are the gaps code
    already classified, which carry no picture because there is none to look at
    — asking a model to judge an image that does not exist is how a "missing"
    becomes a hallucinated verdict.
    """
    acceptance = acceptance if isinstance(acceptance, dict) else {}
    items, blocks, notes = [], [], []
    for player in reviewable(players)[:limit]:
        item = {"item_id": str(player.get("id")), "domain": "photo",
                "name": player.get("name"), "state": "has_photo",
                "reason_code": player.get("photo_match"),
                "detail": player.get("position_group")}
        items.append((item, player))
    for kind in ("retryable", "needs_roster_facts", "namesake_blocked", "ambiguous"):
        for entry in acceptance.get(kind) or []:
            if not isinstance(entry, dict):
                continue
            items.append(({"item_id": str(entry.get("player_id")), "domain": "photo_gap",
                           "name": entry.get("name"), "state": kind,
                           "reason_code": entry.get("reason_code"), "detail": None}, None))
    chosen = items[:limit]
    board = []
    for item, player in chosen:
        if player is None:
            board.append(item)
            continue
        try:
            data, _, _ = await _download(player["photo_url"], allowed_hosts=allowed_hosts)
        except IngestionError as exc:
            notes.append(f"{player.get('name') or player.get('id')}：{exc}")
            continue
        except Exception:
            notes.append(f"{player.get('name') or player.get('id')}：照片暂时无法读取，稍后自动重试。")
            continue
        media_type = media_type_of(data)
        block = image_block(media_type, data) if media_type else None
        if block is None:
            notes.append(f"{player.get('name') or player.get('id')}：来源返回的内容不是可用的图片格式。")
            continue
        board.append({**item, "image_index": len(blocks)})
        blocks.append(block)
    return board, blocks, notes


async def review_photos(store, players, *, acceptance=None, requester=None, timeout=90, allowed_hosts=None):
    """One bounded round: look at the sourced photos, and rule on the gaps.

    Returns ``(decisions, notes)``. A photo that cannot be read is a note, not a
    decision — turning a transport fault into "this picture is bad" would delete
    a good photo over a network error.
    """
    # The model check comes first: downloading a squad's pictures to hand them to
    # nobody would spend a source's bandwidth for no answer.
    if review_loop.review_config(store) is None:
        return [], []
    board, blocks, notes = await build_board(players, acceptance, allowed_hosts=allowed_hosts)
    if not board:
        return [], notes
    decisions = await review_loop.review(store, board, images=blocks, requester=requester,
                                         timeout=timeout, session="photo_review")
    return decisions, notes
