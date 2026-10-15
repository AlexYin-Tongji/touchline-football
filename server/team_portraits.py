"""Bounded source-backed portraits; never search by a loose surname.

Two kinds of checked directory are supported. A club that marks up each player
with its own stable classes uses ``cards``; a club whose page only exposes a
player link, a plain name and a portrait uses ``entries``. Either way an image
is accepted only when the roster's full name is unique on both sides AND the
shirt number matches, so a photo can never attach to the wrong person. This
adds images, never squad membership or facts.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment

from .ingestion import IngestionError, now, published_time, source_error_details
from .community_portraits import COMMUNITY_PHOTO_MATCHES, REFRESHABLE_COMMUNITY_MATCHES
from .research_transport import fetch_reviewed_portrait_document as fetch_document

DIRECTORIES = {
    "barcelona": {
        "url": "https://www.fcbarcelona.com/en/football/first-team/players",
        "host": "www.fcbarcelona.com", "credit": "FC Barcelona 官方球员目录",
        "profile_prefix": "/en/football/first-team/players/",
        "image_prefix": "/photo-resources/",
        "cards": {"selector": "a.team-person[href]",
                  "name": ".team-person__first-name, .team-person__last-name",
                  "number": ".team-person__number", "image": "img.team-person__image"},
    },
    "juventus": {
        "url": "https://www.juventus.com/en/teams/first-team-men/squad/",
        "host": "www.juventus.com", "credit": "尤文图斯官方球员目录",
        "profile_prefix": "/en/teams/first-team-men/squad/",
        "image_prefix": "/images/image/",
        # Coach and staff are separate containers, so only player cards are read.
        "entries": {"selector": "div.jcom-player-item",
                    "profile": "a[href]", "name": "img[alt]",
                    "number": ".jcom-player-info__number",
                    "image": "img[src], img[data-src]"},
    },
    "tottenham-hotspur": {
        "url": "https://www.tottenhamhotspur.com/teams/mens/squad",
        "host": "www.tottenhamhotspur.com", "credit": "Tottenham Hotspur 官方球员目录",
        "profile_prefix": "/player/", "image_prefix": "/photo-resources/",
        "image_hosts": {"resources.thfc.pulselive.com"},
        "cards": {"selector": "a.o-person-pod__inner[href]",
                  "name": ".o-person-pod__name", "number": ".o-person-pod__number",
                  "image": ".o-person-pod__image--main img", "lazy_comments": True},
    },
}
CACHE_SECONDS = 86400
RETRY_SECONDS = 3600
# Each attempted player costs several paced Wikimedia requests (2.2s apart), so
# a batch must fit the 120s phase budget in workflows._team_cycle. At roughly
# 6-7s per player, 12 players is a safe batch; the cursor carries the rest into
# the next run. Raising this without raising that timeout truncates the batch
# and reports it as "interrupted", which is what made photos look missing.
PLAYER_PAGE_LIMIT = 12
PLAYER_PAGE_CONCURRENCY = 3
# Each source has its own cursor. An outage must not turn one team refresh
# into a full-squad burst against the fallback source.
COMMUNITY_PAGE_LIMIT = 12
# Measured mean cost of one player attempt, used to size a batch against the
# time actually left rather than against a fixed ceiling. Raising the ceiling
# alone is what truncated batches and reported them as "interrupted".
BATCH_SECONDS_PER_PLAYER = 6.5


def batch_limit(remaining_seconds, *, ceiling=PLAYER_PAGE_LIMIT):
    """How many players fit in the time left, never fewer than one.

    A caller with no deadline passes nothing and gets the historical ceiling,
    so existing behaviour is unchanged where no clock is known.
    """
    if remaining_seconds is None:
        return ceiling
    return max(1, min(ceiling, int(remaining_seconds // BATCH_SECONDS_PER_PLAYER)))
PLAYER_PHOTO_FIELDS = ("photo_url", "photo_credit", "photo_source_url", "photo_updated_at", "photo_match",
                       "photo_file_page", "photo_license", "photo_restrictions", "photo_reason",
                       "retryable", "reason_code", "namesake_blocked")
# The one field above that is not a photo: it says the source answered with a
# same-named stranger. It belongs in this tuple because it must be cleared by
# exactly the same events that clear a photo — a row that ends up with someone
# else's picture, or with no picture and a different reason, must not keep
# claiming a namesake blocked it.
PHOTO_METADATA_FIELDS = tuple(field for field in PLAYER_PHOTO_FIELDS if field != "namesake_blocked")
# A photo carrying one of these matches was checked against the roster's own
# name AND number, so a later refresh may re-derive or replace it.
REFRESHABLE_PHOTO_MATCHES = frozenset({"exact_name_and_number", "wikimedia_verified_club",
                                       *REFRESHABLE_COMMUNITY_MATCHES})


def espn_headshot(athlete, *, fetched_at):
    """Use an image published in the same checked athlete record, by its ID.

    This is independent of club popularity, spelling and shirt number. Never
    manufacture a CDN URL: the roster must supply it, and the soccer image's
    ID must agree with the athlete that supplied it.
    """
    if not isinstance(athlete, dict):
        return {}
    headshot = athlete.get("headshot")
    identity = str(athlete.get("id") or "")
    if not isinstance(headshot, dict) or not re.fullmatch(r"[0-9]{1,10}", identity):
        return {}
    if any(record.get(flag) is True for record in (athlete, headshot)
           for flag in ("private", "deleted", "restricted")):
        return {}
    image = headshot.get("href")
    if (not isinstance(image, str) or not image or len(image) > 2048
            or any(ord(char) <= 32 or char == "\\" for char in image)):
        return {}
    try:
        parts = urlsplit(image)
        match = re.fullmatch(r"/i/headshots/soccer/players/(?:full|thumb)/([0-9]{1,10})\.(?:png|jpe?g|webp)", parts.path)
        if (parts.scheme != "https" or parts.hostname != "a.espncdn.com" or parts.port not in {None, 443}
                or parts.username or parts.password or parts.fragment or not match or match[1] != identity):
            return {}
        # Public image sizing is supported; credentials, arbitrary transforms
        # and unknown query parameters cannot enter a browser image request.
        if any(key not in {"w", "h", "scale"} or not value.isdigit() or not 1 <= int(value) <= 2000
               for key, value in parse_qsl(parts.query, keep_blank_values=True)):
            return {}
    except (TypeError, ValueError):
        return {}
    return {"photo_url": image, "photo_credit": "ESPN 球员资料",
            "photo_source_url": f"https://www.espn.com/soccer/player/_/id/{identity}",
            "photo_updated_at": fetched_at, "photo_match": "espn_provider_id",
            "photo_license": "未声明开放许可",
            "photo_restrictions": "ESPN 公开球员照片；图片权利归原作者或发布方。"}


def _recheck_espn_photos(players, *, revoked_ids=()):
    """A cached image still has to belong to the row's current provider ID."""
    for player in players:
        if player.get("photo_match") not in {"espn_provider_id", "espn_directory_provider_id"}:
            continue
        photo = espn_headshot({"id": player.get("provider_id"), "headshot": {"href": player.get("photo_url")}},
                              fetched_at=player.get("photo_updated_at"))
        withdrawn = (str(player.get("provider_id") or "") in revoked_ids
                     or "espn" in (player.get("photo_blocked_sources") or []))
        if not photo or player.get("photo_source_url") != photo["photo_source_url"] or withdrawn:
            for field in PLAYER_PHOTO_FIELDS:
                player.pop(field, None)
            player.update(photo_url="", photo_credit="",
                          photo_reason="ESPN 当前记录已撤回这张照片，等待来源重新公开核验。" if withdrawn else
                                       "来源照片与球员编号不一致，需要重新核对。",
                          retryable=True, reason_code="source_withdrawn" if withdrawn else "identity_revalidation")


class PortraitDirectoryError(IngestionError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def _apply_photo(player, photo):
    """A new checked source replaces all metadata — unless it is a weaker one.

    ``photo_outranks`` is consulted here rather than at each call site so that a
    source cannot replace a stronger photo by reaching this function through a
    path nobody remembered to guard. The return value says whether the row was
    written, which lets a caller report "kept the better source" instead of
    silently claiming an update.
    """
    if not photo_outranks(photo.get("photo_match"), player.get("photo_match")):
        return False
    for field in PLAYER_PHOTO_FIELDS:
        player.pop(field, None)
    player.update({field: photo[field] for field in PLAYER_PHOTO_FIELDS if field in photo})
    player["photo_updated_at"] = photo.get("fetched_at") or photo.get("photo_updated_at") or now()
    return True


def _record_kept_source(summary, player, photo):
    """Note a source that answered with a picture and was outranked anyway.

    This is the case the return value of ``_apply_photo`` exists to expose. A
    weaker source deriving a perfectly good photo for a row ESPN already answered
    is *correctly* refused — but the caller used to discard the refusal, so the
    pass reported the same numbers whether the lookup was refused for this reason
    or never returned anything at all. "Kept the stronger source" and "the source
    had nothing" are different facts about the run, and only one of them means
    the work is settled.

    Recorded as a count plus the source names, never as a per-player list: the
    reader needs to know the pass behaved, not which rows were skipped, and a
    player id here would be an identity claim this function has not verified.
    """
    current = photo_source_rank(player.get("photo_match"))
    incoming = photo_source_rank(photo.get("photo_match"))
    # Lower rank wins, the same direction ``photo_outranks`` uses. A source that
    # would have been written is not a refusal, and an unranked one is not a
    # comparison this can make.
    if incoming is None or current is None or incoming <= current:
        return
    summary["kept_stronger_source_count"] = summary.get("kept_stronger_source_count", 0) + 1
    pairs = summary.setdefault("kept_stronger_source", [])
    pair = {"kept": player.get("photo_match"), "skipped": photo.get("photo_match")}
    if pair not in pairs:
        pairs.append(pair)


def _merge_kept_source(summary, kept):
    """Fold the accumulator into the pass summary, if it recorded anything."""
    if kept.get("kept_stronger_source_count"):
        summary.update(kept)
    return summary


# Which source a player's portrait comes from, strongest first. This is the one
# place the order is decided, so acquiring a photo and replacing one already on
# the row cannot disagree about which source wins.
#
# The order is measured, not preferred, and it was re-measured on 2026-10-15.
# The earlier ordering put the community database last on the strength of a
# reading that turned out to be about the wrong path: what had been measured
# as "name search" was the *surname* fallback and the roster endpoint, and both
# are genuinely bad. A full-name query is a different question to the same
# endpoint and a good one — the publisher ranks rather than lists, so the
# response is one record and the whole decision is whether one identity fact
# agrees. Across 107 players at Arsenal, Real Madrid, Bayern Munich and Inter
# it matched 81-90% with an exact birth date, produced zero wrong-person
# matches, and every refusal was a real namesake or a stale record.
#
# So TheSportsDB now leads. It costs one paced request per player and answers
# for most of a squad, which the club directories (useful at three clubs, 81%
# where configured) and Wikidata (11%, ~3 min/club) never did.
PHOTO_SOURCE_ORDER = (
    # Full name plus a birth date that agrees to the day. The tier the
    # 2026-10-15 measurement was about; 81-90% of a squad, zero misattribution.
    "community_name_and_birthdate",
    # The club's own directory, name + number. Still the strongest evidence
    # available — a club stating its own squad — but it only exists at a
    # handful of clubs, so it is no longer the default first pass.
    "exact_name_and_number",
    # Same ESPN id as the roster, plus a Commons file whose licence was checked.
    "wikidata_espn_provider_id",
    # The remaining community tiers, weakest last. Flattened onto one rank
    # below, so only this source may re-derive over itself.
    "community_espn_provider_id",      # same athlete id as the roster carries
    "community_name_and_nationality",  # one unique record, nationality agrees
    "community_name_and_birth_year",   # full name, birth year only
    "community_surname_and_birthdate",  # publisher's spelling, exact birth date
    "wikimedia_verified_club",        # free licence, identity + club verified
    "espn_directory_provider_id",     # ESPN's own player page, id-bound
    "espn_provider_id",               # rides on the roster payload, id-bound
)
# The five weaker community tiers share one rank — the position of the first of
# them — so a later tier cannot outrank an earlier one from the same publisher.
#
# The birth-date tier is deliberately *not* in that set, and the reason is the
# rank arithmetic: ``_COMMUNITY_RANK`` is read off this tuple, so widening the
# set to include the tier now at index 0 would collapse every community tier
# onto rank 0 and make a surname guess equal to a club's own verified directory.
# It is also the right answer on the merits — a full name with an agreeing birth
# date is a stronger claim than the tiers below it, and the one tier that must
# not be able to overwrite it is the weaker evidence from its own publisher.
_COMMUNITY_RANK = PHOTO_SOURCE_ORDER.index("community_espn_provider_id")
PHOTO_SOURCE_RANK = {match: (_COMMUNITY_RANK if match in COMMUNITY_PHOTO_MATCHES
                             and match != "community_name_and_birthdate" else rank)
                     for rank, match in enumerate(PHOTO_SOURCE_ORDER)}


def photo_source_rank(match):
    """The strength of the source that produced a row's picture, if any."""
    return PHOTO_SOURCE_RANK.get(str(match or ""))


def photo_outranks(incoming, current):
    """Whether a new result may replace the picture already on the row.

    Equal rank is allowed on purpose: the same source re-deriving its own
    answer is a refresh, which is what a manual retry asks for. Only a weaker
    source being applied over a stronger one is refused — that is the case the
    old code allowed, and it is why a community photo could land on a row ESPN
    had answered and why an official card could strip the licence metadata a
    Wikimedia photo had to carry.
    """
    if not current:
        return True
    incoming_rank, current_rank = photo_source_rank(incoming), photo_source_rank(current)
    if incoming_rank is None:
        return False
    if current_rank is None:
        return True
    return incoming_rank <= current_rank


def _apply_source_verdict(player, photo):
    """Write a source's durable "not this player" answer onto the row.

    A verdict and a transport fault are different facts and this writes only the
    first. A fault leaves the row looking merely un-attempted, which is right —
    nothing has been learned and the next pass should try again. A verdict is
    the source having been asked and answered, and leaving it off the row makes
    the acceptance report count it as never attempted, so the same players are
    asked again forever.

    The photo already on the row is not touched. A source reaching a negative
    answer about a player it was never able to identify says nothing about a
    picture a *different* source proved, and clearing it here would be the
    cache-window-read-as-verdict mistake in a new place. Only the reason
    travels, and only when the row has no stronger picture to protect.
    """
    if photo.get("retryable"):
        return
    next_rank = photo_source_rank(photo.get("photo_match"))
    current_rank = photo_source_rank(player.get("photo_match"))
    if current_rank is not None and (next_rank is None or current_rank < next_rank):
        # A stronger source is already on the row. This refusal is about a
        # weaker tier and cannot speak to it.
        return
    if not player.get("photo_url"):
        player["photo_reason"] = photo.get("photo_reason") or "社区球员库里没有这名球员的可展示照片。"
        player["retryable"] = False
        player["reason_code"] = photo.get("reason_code") or "community_missing"
        _copy_namesake(player, photo)


def _copy_namesake(player, photo):
    """Keep a namesake block on the row, and drop it when this answer isn't one.

    The community resolver carries ``namesake_blocked`` beside its own reason
    code. Copying only the code made the row look like an ordinary ambiguous
    name, which the planner will offer to retry. The flag is what says the
    truncated search returned a different person.
    """
    if photo.get("namesake_blocked"):
        player["namesake_blocked"] = True
    else:
        player.pop("namesake_blocked", None)


def _community_failure(player, photo, preferred=None):
    """Expose the source that actually failed, preserving an independent fault."""
    community_reason = "TheSportsDB 社区照片来源：" + (photo.get("photo_reason") or "社区球员库暂时不可达，稍后自动重试。")
    reasons = []
    if preferred and preferred.get("status") == "unavailable":
        reasons.append("维基媒体照片来源：" + (preferred.get("photo_reason") or "维基媒体暂时不可达，稍后自动重试。"))
    reasons.append(community_reason)
    player.update(photo_reason="；".join(reasons), retryable=bool(photo.get("retryable", True)),
                  reason_code=photo.get("reason_code") or "community_unavailable")
    return community_reason


def _source_refutes_photo(photo):
    """Whether a non-photo answer is a verdict the source actually reached.

    The row used to be cleared for any answer that was not a transport failure,
    which grouped two different things together:

    * the source was never asked anything about this player's face — either the
      request never got an answer (a timeout, a throttle, an outage), or the
      roster entry lacked the name or the country/birth date the lookup needs,
      so ``resolve_portrait`` returned before spending a request at all. Neither
      says anything about the photo already on the row;
    * the source was asked and answered about this player — it holds nobody by
      that name, it cannot separate the candidates, or it refused the image.

    Only the second kind is a verdict, and a verdict reached while re-checking
    a player the caller named *by id* may take the verified photo off the row:
    the caller asked for that player specifically, so a fresh answer speaking to
    that player is evidence. A transport fault is not, which is why it is
    excluded here as well as in the calling branch — a source that went quiet
    must never be able to revoke a photo it proved while it was reachable.
    """
    if photo.get("status") == "unavailable":
        return False
    return str(photo.get("reason_code") or "") not in {"missing_name", "missing_nationality"}


def _recheck_community_photos(store, players, team_id):
    """A roster's old image cannot bypass the current cache identity check.

    A stale cache entry is not evidence against the photo, which is the
    distinction this function used to miss: anything the reuse window had
    closed was treated as a refuted identity, so the row's photo was cleared
    and re-acquired on a seven-day clock. A validated or self-consistent photo
    is now kept, and only a completion that no longer proves this roster entry
    is withdrawn.

    What still withdraws it, and must, is a *newer* verdict from the same
    source: a cached ``missing`` or ``ambiguous`` reaching past the completion
    the photo rests on is the source contradicting itself, and the photo does
    not get to outlive that.
    """
    from .community_portraits import cached_portrait, current_proof, portrait_key, _age_seconds
    for player in players:
        if player.get("photo_match") not in COMMUNITY_PHOTO_MATCHES:
            continue
        cached = cached_portrait(store, player, team_id)
        if cached and cached.get("status") == "completed":
            _apply_photo(player, cached)
            continue
        stored = store.get_meta(portrait_key(player, team_id), {})
        verdict_is_newer = (stored.get("status") in {"missing", "ambiguous"}
                            and _age_seconds(stored.get("fetched_at")) < _age_seconds(player.get("photo_updated_at")))
        if verdict_is_newer:
            for field in PLAYER_PHOTO_FIELDS:
                player.pop(field, None)
            player.update(photo_url="", photo_credit="",
                          photo_reason=stored.get("photo_reason") or "照片身份校验已更新，需要重新核对社区照片。",
                          retryable=bool(stored.get("retryable")),
                          reason_code=stored.get("reason_code") or "identity_revalidation")
            # The flag lives beside the reason code. Dropping only the code made
            # a namesake block look like an ordinary ambiguous name.
            _copy_namesake(player, stored)
        elif current_proof(store, player, team_id):
            # Keep the verified photo and drop only the stale reason left on
            # the row. The photo is still the one a source proved; nothing has
            # said otherwise.
            player.pop("photo_reason", None)
            player.pop("retryable", None)
            player.pop("reason_code", None)
        else:
            for field in PLAYER_PHOTO_FIELDS:
                player.pop(field, None)
            player.update(photo_url="", photo_credit="",
                          photo_reason="照片身份校验已更新，需要重新核对社区照片。",
                          retryable=True, reason_code="identity_revalidation")


def _recheck_wikidata_photos(store, players):
    """A Wikidata photo stays only while its ESPN id still proves this row."""
    from .wikidata_portraits import current_proof
    for player in players:
        if player.get("photo_match") != "wikidata_espn_provider_id":
            continue
        if current_proof(store, player):
            continue
        for field in PLAYER_PHOTO_FIELDS:
            player.pop(field, None)
        player.update(photo_url="", photo_credit="",
                      photo_reason="照片身份校验已更新，需要重新核对维基数据照片。",
                      retryable=True, reason_code="identity_revalidation")


def _recheck_wikimedia_photos(store, players, team_id):
    """Current source verdicts govern saved roster images and their links too."""
    from .wikimedia_portraits import cached_portrait
    for player in players:
        if player.get("photo_match") != "wikimedia_verified_club":
            continue
        cached = cached_portrait(store, player, team_id)
        if cached and cached.get("status") == "completed":
            _apply_photo(player, cached)
            continue
        for field in PLAYER_PHOTO_FIELDS:
            player.pop(field, None)
        player.update(photo_url="", photo_credit="",
                      photo_reason=(cached or {}).get("photo_reason") or "照片来源与许可需要重新核对。",
                      retryable=bool(cached.get("retryable", cached.get("status") == "unavailable")) if cached else True,
                      reason_code=(cached or {}).get("reason_code") or "identity_revalidation")


async def _portrait_document(store, url, *, allowed_hosts, max_body, content_types=("text/html", "xhtml")):
    """Share physical workers; cancellation stops readers, never frees a busy slot."""
    if not hasattr(store, "_player_portrait_semaphore"):
        store._player_portrait_semaphore = asyncio.Semaphore(PLAYER_PAGE_CONCURRENCY)
    if not hasattr(store, "_portrait_pending"):
        store._portrait_pending = {}
        store._portrait_workers = set()
    pending = store._portrait_pending
    entry = pending.get(url)
    if entry is None:
        entry = {"started": False, "waiters": 0}
        pending[url] = entry

        async def perform():
            capacity = store._player_portrait_semaphore
            await capacity.acquire()
            entry["started"] = True
            try:
                worker = asyncio.create_task(asyncio.to_thread(fetch_document, url,
                    allowed_hosts=allowed_hosts, max_body=max_body, content_types=content_types))
            except BaseException:
                capacity.release()
                raise
            store._portrait_workers.add(worker)

            def finished(task):
                # This callback belongs to the shielded worker, not its reader.
                # A timeout/cancelled reader cannot release physical capacity or
                # remove same-URL sharing while the synchronous request runs.
                capacity.release()
                store._portrait_workers.discard(task)
                if pending.get(url) is entry:
                    pending.pop(url, None)
                if not task.cancelled():
                    task.exception()

            worker.add_done_callback(finished)
            return await asyncio.shield(worker)

        entry["task"] = asyncio.create_task(perform())

        def reader_done(task):
            if not entry["started"] and pending.get(url) is entry:
                pending.pop(url, None)
            if not task.cancelled():
                task.exception()

        entry["task"].add_done_callback(reader_done)
    entry["waiters"] += 1
    try:
        return await asyncio.wait_for(asyncio.shield(entry["task"]), timeout=22)
    finally:
        entry["waiters"] -= 1
        if not entry["waiters"] and not entry["started"]:
            # A request abandoned while queued should never start networking.
            entry["task"].cancel()
            if pending.get(url) is entry:
                pending.pop(url, None)
            await asyncio.gather(entry["task"], return_exceptions=True)


def normalized_name(value):
    text = "".join(c for c in unicodedata.normalize("NFKD", str(value or "")).casefold()
                   if not unicodedata.combining(c))
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


def _elapsed(value):
    parsed = published_time(value)
    return (datetime.now(timezone.utc) - datetime.fromisoformat(parsed)).total_seconds() if parsed else float("inf")


def _public_url(value, config, *, image=False):
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        url = urljoin(config["url"], value)
        parsed = urlsplit(url)
        hosts = config.get("image_hosts", {config["host"]}) if image else {config["host"]}
        if (parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password
                or parsed.port not in {None, 443} or parsed.fragment):
            return ""
        if image:
            if not parsed.path.startswith(config["image_prefix"]) or not parsed.path.lower().endswith(
                    (".png", ".jpg", ".jpeg", ".webp")):
                return ""
        elif not parsed.path.startswith(config["profile_prefix"]) or parsed.path.rstrip("/") == \
                config["profile_prefix"].rstrip("/"):
            # A bare directory link is not a player's own page.
            return ""
        return url
    except ValueError:
        return ""


def _card_image(node, markup):
    image = node.select_one(markup["image"]) if node else None
    if image is None and markup.get("lazy_comments"):
        # Spurs keeps each card's main portrait inside a lazy-picture comment.
        # Read only that card and its main picture, never its hover/action shot.
        for comment in node.find_all(string=lambda value: isinstance(value, Comment)):
            image = BeautifulSoup(str(comment), "html.parser").select_one(markup["image"])
            if image is not None:
                break
    return image.get("data-image-src") or image.get("src") if image else ""


def _parse_cards(soup, config):
    """A directory that marks up each player with its own stable classes."""
    markup = config["cards"]
    entries = []
    for card in soup.select(markup["selector"]):
        names = card.select(markup["name"])
        number = card.select_one(markup["number"])
        entries.append({
            "profile": card.get("href"),
            "name": " ".join(node.get_text(" ", strip=True) for node in names).strip(),
            "number": number.get_text(strip=True) if number else "",
            "image": _card_image(card, markup),
        })
    return entries


def _parse_entries(soup, config):
    """A directory that only exposes a player link, a name and a portrait.

    The name comes from the portrait's own alt text, which the club writes for
    this player, rather than from concatenated DOM fragments that may include a
    position, a nationality or a sponsor.
    """
    markup = config["entries"]
    entries = []
    for node in soup.select(markup["selector"]):
        profile = node.select_one(markup["profile"])
        image = node.select_one(markup["image"])
        if image is None:
            continue
        number = node.select_one(markup["number"])
        entries.append({
            "profile": profile.get("href") if profile else "",
            "name": str(image.get("alt") or "").strip(),
            "number": number.get_text(strip=True) if number else "",
            "image": image.get("src") or image.get("data-src") or "",
        })
    return entries


def parse_directory(body, team_id):
    config = DIRECTORIES.get(team_id)
    if not config:
        return []
    soup = BeautifulSoup(body, "html.parser")
    raw = _parse_cards(soup, config) if config.get("cards") else _parse_entries(soup, config)
    cards = []
    for entry in raw:
        profile = _public_url(entry["profile"], config)
        image_url = _public_url(entry["image"], config, image=True)
        name = re.sub(r"\s+", " ", str(entry["name"] or "")).strip()
        number = str(entry["number"] or "").strip()
        # A number is required: it is the second identity fact that stops a
        # same-name squad member from taking another player's portrait.
        if not profile or not name or not number.isdigit() or not image_url:
            continue
        cards.append({"name": name, "normalized_name": normalized_name(name), "number": number,
                      "photo_url": image_url, "source_url": profile})
    if not cards or len(cards) > 100:
        raise PortraitDirectoryError("官网球员目录暂未提供可核对的头像。", "portrait_directory_empty")
    return cards


async def portrait_directory(store, team_id, *, force=False):
    config = DIRECTORIES.get(team_id)
    if not config:
        return {}
    if not hasattr(store, "_portrait_locks"):
        store._portrait_locks = {}
    lock = store._portrait_locks.setdefault(team_id, asyncio.Lock())
    async with lock:
        key = "team_portraits_v1_" + team_id
        previous = store.get_meta(key, {})
        if not force and (previous.get("cards") and _elapsed(previous.get("fetched_at")) < CACHE_SECONDS
                          or _elapsed(previous.get("last_attempt")) < RETRY_SECONDS):
            return previous
        attempted = now()
        try:
            # Fixed host and one directory only; redirects remain on this host.
            body, final_url, _ = await _portrait_document(store, config["url"],
                allowed_hosts={config["host"]}, max_body=2 * 1024 * 1024)
            # Compare the two addresses the same way. Stripping the trailing
            # slash from one side only made every configured URL that ends in
            # "/" fail this check forever: ".../squad" never equals
            # ".../squad/", so the club reported "the address changed" and
            # yielded zero portraits without anything having changed at all.
            if final_url.rstrip("/") != config["url"].rstrip("/"):
                raise PortraitDirectoryError("官网球员目录地址已变化，暂不更新头像。", "portrait_directory_changed")
            cards = parse_directory(body, team_id)
            result = {"team_id": team_id, "cards": cards, "source_url": config["url"],
                      "fetched_at": now(), "last_attempt": attempted, "status": "completed"}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The last checked photos remain eligible only after matching this
            # refresh's confirmed player name AND number again.
            error = ({"code": exc.code, "stage": "parse", "message": str(exc)}
                     if isinstance(exc, PortraitDirectoryError) else source_error_details(exc))
            result = {**previous, "last_attempt": attempted, "status": "unavailable",
                      "error": error, "error_code": error["code"], "last_error": error["message"], "retryable": True}
        store.set_meta(key, result)
        return result


async def enrich_portraits(store, snapshot, *, force=False):
    team_id = snapshot.get("team_id")
    if team_id not in DIRECTORIES or not any(not player.get("photo_url") or player.get("photo_match") in
            REFRESHABLE_PHOTO_MATCHES for player in snapshot.get("roster", [])):
        return snapshot
    directory = await portrait_directory(store, team_id, force=force)
    return _apply_directory(snapshot, directory)


def apply_cached_portraits(store, snapshot):
    """Reuse checked cards without making a core roster fetch wait on HTML."""
    from .community_portraits import cached_portrait as cached_community
    from .wikidata_portraits import cached_portrait as cached_wikidata
    from .wikimedia_portraits import cached_portrait

    _recheck_espn_photos(snapshot.get("roster", []))
    _recheck_community_photos(store, snapshot.get("roster", []), snapshot.get("team_id"))
    _recheck_wikidata_photos(store, snapshot.get("roster", []))
    _recheck_wikimedia_photos(store, snapshot.get("roster", []), snapshot.get("team_id"))
    snapshot = _apply_espn_directory(snapshot, _espn_directory_with_revocations(store,
        store.get_meta("espn_portraits_v1_" + snapshot.get("team_id", ""), {}),
        player_ids={str(row.get("provider_id") or "") for row in snapshot.get("roster", [])}))
    snapshot = _apply_directory(snapshot, store.get_meta("team_portraits_v1_" + snapshot.get("team_id", ""), {}))
    for player in snapshot.get("roster", []):
        _apply_cached_reading(store, player, snapshot.get("team_id"))
        wikidata = cached_wikidata(store, player)
        if wikidata and wikidata.get("status") == "completed" and wikidata.get("photo_url"):
            # Outranks a community cutout and an ESPN roster headshot. A club
            # directory photo, already applied above, outranks this one.
            _apply_photo(player, wikidata)
        if player.get("photo_url"):
            continue
        cached = cached_portrait(store, player, snapshot.get("team_id"))
        if cached and cached.get("status") == "completed" and all(cached.get(field) for field in
                ("photo_url", "photo_credit", "photo_source_url", "photo_file_page", "photo_license")):
            _apply_photo(player, cached)
            continue
        community = cached_community(store, player, snapshot.get("team_id"))
        if community and community.get("status") == "completed" and community.get("photo_url"):
            _apply_photo(player, community)
            continue
        if community and community.get("status") == "unavailable":
            _community_failure(player, community, cached)
            continue
        verdict = cached if cached and cached.get("photo_reason") else community
        if verdict and verdict.get("photo_reason"):
            # Carry the known non-photo verdict onto the rebuilt row too, so a
            # refreshed roster still explains why a player has no image instead
            # of showing an empty placeholder with no reason.
            player["photo_reason"] = verdict["photo_reason"]
            player["retryable"] = bool(verdict.get("retryable"))
            player["reason_code"] = verdict.get("reason_code") or "rejected"
            _copy_namesake(player, community or {})
        elif not player.get("photo_reason"):
            # Neither source holds a verdict: both caches expired, or neither has
            # been asked yet. Say that rather than leaving a placeholder with no
            # explanation, which reads as "this player has no photo" when the
            # truth is "we have not finished checking".
            player["photo_reason"] = "照片来源尚未给出结论，下次更新会重新核对。"
            player["retryable"] = True
            player["reason_code"] = "not_yet_resolved"
    return snapshot


def _apply_cached_reading(store, player, team_id):
    from .wikimedia_portraits import cached_reading
    from .player_names import with_reading
    with_reading(player, source_reading=cached_reading(store, player, team_id), team_id=team_id)


def photo_queue(store, snapshot, *, limit=60):
    """What photo work remains for one team, and why each player is still open.

    This exists because the batch loop could only ever answer "who is next by
    cursor position". Measured on a real squad, every missing player carried
    ``not_yet_resolved`` — none had been *rejected* — so the queue was the
    whole problem: 30 players, 12 per pass, and the cursor is pure rotation
    with no notion of which twelve are worth asking about first.

    The report is deliberately narrow. It carries no image URL, no licence and
    no identity evidence: a caller may decide *who to ask about*, never *what
    the answer is*. Identity stays with the source that can prove it.
    """
    roster = snapshot.get("roster") or []
    team_id = snapshot.get("team_id", "")
    season_id = snapshot.get("season_id", "")
    untried, settled = [], []
    for row in roster[:120]:
        entry = {"player_id": str(row.get("provider_id") or row.get("id") or ""),
                 "name": row.get("name") or "", "position_group": row.get("position_group") or "",
                 # Which identity facts exist locally decides whether either
                 # source can be asked at all, which is different from the
                 # source having answered "no".
                 "has_nationality": bool(row.get("nationality")),
                 "has_birth_date": bool(row.get("birth_date"))}
        if row.get("photo_url"):
            continue
        reason = row.get("reason_code") or "not_yet_resolved"
        # A namesake block is carried as its own fact rather than folded into
        # the reason code, because the code is still the source's own verdict
        # about this roster entry. It matters here for a different reason than
        # it does in the acceptance report: a player blocked by a namesake is
        # not retryable against this source, but they are also not unreachable
        # — another source can still answer, and the caller has to be able to
        # tell those two apart before deciding whether to act.
        if row.get("namesake_blocked"):
            entry["namesake_blocked"] = True
            reason = "community_namesake_blocked"
        entry["reason_code"] = reason
        if reason == "not_yet_resolved" or row.get("retryable"):
            untried.append(entry)
        else:
            settled.append(entry)
    # A player with neither a nationality nor a birth date can never be matched
    # by the community source: it refuses before making a request. Surfacing
    # the count separately says "asking again will not help" rather than leaving
    # them indistinguishable from players merely not reached yet.
    no_facts = [entry for entry in untried
                if not entry["has_nationality"] and not entry["has_birth_date"]]
    available = sum(bool(row.get("photo_url")) for row in roster)
    return {
        "team_id": team_id, "season_id": season_id,
        "total": len(roster), "available": available, "missing": len(roster) - available,
        "untried": untried[:limit], "untried_count": len(untried),
        "settled": settled[:limit], "settled_count": len(settled),
        "no_identity_facts_count": len(no_facts),
        "cursors": {"community": _cursor_position(store, "community_cursor_v1", team_id, season_id, roster),
                    "wikimedia": _cursor_position(store, "wikimedia_cursor_v1", team_id, season_id, roster)},
        "batch_budget": {"community_limit": COMMUNITY_PAGE_LIMIT, "wikimedia_limit": PLAYER_PAGE_LIMIT,
                         "seconds_per_player": BATCH_SECONDS_PER_PLAYER},
        "hint": ("有 {0} 名球员缺少国籍和出生日期，两个照片来源都无法核对。"
                 "先补全阵容资料（maintain_team_data）才能恢复他们的照片核对路径。").format(len(no_facts))
                if no_facts else None,
    }


def _ordered_selection(selected, order, roster):
    """Reorder a selection by someone else's priority, without adding to it.

    Every id must already be in this roster, so a caller cannot introduce a
    player that the roster does not contain. Rows the order does not mention
    keep their original relative order and follow it — the order is a
    preference among real work, not a filter that hides the rest.
    """
    if not isinstance(order, (list, tuple)):
        raise IngestionError("补充照片的顺序必须是球队阵容内的球员列表。")
    by_key = {}
    for row in selected:
        for key in ("id", "provider_id"):
            value = row.get(key)
            if value:
                by_key.setdefault(str(value), row)
    ranked, seen = [], set()
    for value in order:
        row = by_key.get(str(value))
        if row is None:
            raise IngestionError("只能补充当前已核对阵容内的球员照片。")
        identity = id(row)
        if identity not in seen:
            seen.add(identity)
            ranked.append(row)
    return ranked + [row for row in selected if id(row) not in seen]


def _cursor_position(store, prefix, team_id, season_id, roster):
    if not hasattr(store, "get_meta") or not roster:
        return {"next": 0, "size": len(roster)}
    stored = store.get_meta(f"{prefix}_{team_id}_{season_id}", {})
    try:
        return {"next": int(stored.get("next", 0)) % len(roster), "size": len(roster)}
    except (TypeError, ValueError):
        return {"next": 0, "size": len(roster)}


def _espn_directory_with_revocations(store, directory, *, player_ids):
    """Athlete IDs are global: a transfer cannot restore another team's old image."""
    revoked = set(directory.get("revoked_ids") or [])
    records = store.get_meta("espn_portrait_visibility_v1", {}).get("records") or {}
    for identity in player_ids:
        state = records.get(identity, {})
        if state.get("status") == "withdrawn":
            revoked.add(identity)
        elif state.get("status") == "public":
            revoked.discard(identity)
    return {**directory, "revoked_ids": sorted(revoked)}


def _apply_espn_directory(snapshot, directory, *, selected=None):
    """Same-provider numeric IDs link photos; the directory never supplies facts."""
    photos = directory.get("photos") or {}
    covered = set(directory.get("covered_ids") or [])
    revoked = set(directory.get("revoked_ids") or [])
    # Revocation is a privacy boundary for the entire snapshot, even when the
    # caller selected only one player for enrichment. Other public sources keep
    # their independently checked images.
    _recheck_espn_photos(snapshot.get("roster", []), revoked_ids=revoked)
    for row in snapshot.get("roster", []) if selected is None else selected:
        identity = str(row.get("provider_id") or "")
        if identity in revoked or "espn" in (row.get("photo_blocked_sources") or []):
            continue
        if row.get("photo_url") and row.get("photo_match") not in {"espn_directory_provider_id"}:
            continue
        photo = photos.get(identity)
        if photo:
            verified = espn_headshot({"id": identity, "headshot": {"href": photo.get("photo_url")}},
                                      fetched_at=photo.get("photo_updated_at"))
            if verified and photo.get("photo_source_url") == verified["photo_source_url"]:
                _apply_photo(row, photo)
                continue
        if identity in covered and row.get("photo_match") == "espn_directory_provider_id":
            for field in PLAYER_PHOTO_FIELDS:
                row.pop(field, None)
            row.update(photo_url="", photo_credit="")
    return snapshot


async def _enrich_espn_directory(store, snapshot, selected, *, force=False):
    """Read at most one source-authored selector per position, for any club."""
    if snapshot.get("provider") != "ESPN":
        return {"attempted_count": 0, "errors": []}
    key = "espn_portraits_v1_" + snapshot["team_id"]
    allowed = {row["provider_id"] for row in snapshot.get("roster", [])}
    previous = _espn_directory_with_revocations(store, store.get_meta(key, {}), player_ids=allowed)
    _apply_espn_directory(snapshot, previous, selected=selected)
    if not selected or not force and (_elapsed(previous.get("fetched_at")) < CACHE_SECONDS
                                      or _elapsed(previous.get("last_attempt")) < 300):
        return {"attempted_count": 0, "errors": []}
    wanted = [row for row in selected if "espn" not in (row.get("photo_blocked_sources") or [])
              and (not row.get("photo_url") or row.get("photo_match") == "espn_directory_provider_id")]
    representatives, groups = [], set()
    for row in wanted:
        group = row.get("position_group") or row.get("provider_id")
        if group not in groups:
            representatives.append(row)
            groups.add(group)
        if len(representatives) >= 4:
            break
    if not representatives:
        return {"attempted_count": 0, "errors": []}
    from .espn_pages import fetch_player_portrait_records
    attempted = now()
    semaphore = asyncio.Semaphore(2)
    async def read(row):
        async with semaphore:
            try:
                return row["provider_id"], await fetch_player_portrait_records(row["provider_id"]), None
            except asyncio.CancelledError:
                raise
            except Exception:
                return row["provider_id"], [], "ESPN 球员照片目录暂时不可读，独立图片来源会继续补充。"
    results = await asyncio.gather(*(read(row) for row in representatives))
    photos = copy.deepcopy(previous.get("photos") or {})
    covered = set(previous.get("covered_ids") or [])
    revoked = set(previous.get("revoked_ids") or [])
    withdrawn, replacements = set(), {}
    successes, errors = 0, []
    for page_id, records, error in results:
        if error:
            errors.append(error)
            continue
        successes += 1
        for record in records:
            identity = record["id"]
            if identity not in allowed:
                continue
            covered.add(identity)
            photo = espn_headshot(record, fetched_at=attempted)
            if photo:
                replacements[identity] = {**photo, "photo_match": "espn_directory_provider_id",
                                         "photo_file_page": "https://www.espn.com/soccer/player/_/id/" + page_id}
            else:
                withdrawn.add(identity)
    # An explicit refusal wins over duplicate positive rows in this pass. A
    # later checked public selector may release the tombstone; roster reuse and
    # an unrelated selector failure cannot do so.
    revoked.update(withdrawn)
    for identity, photo in replacements.items():
        if identity not in withdrawn:
            revoked.discard(identity)
            photos[identity] = photo
    if withdrawn or replacements:
        # Read after the awaited source work so concurrent clubs cannot discard
        # each other's records. Updating this evidence never scans saved squads.
        visibility = store.get_meta("espn_portrait_visibility_v1", {})
        records = copy.deepcopy(visibility.get("records") or {})
        for identity in withdrawn:
            if (records.get(identity, {}).get("checked_at") or "") <= attempted:
                records[identity] = {"status": "withdrawn", "checked_at": attempted}
        for identity in replacements.keys() - withdrawn:
            latest = records.get(identity, {})
            if ((latest.get("checked_at") or "") < attempted
                    or latest.get("checked_at") == attempted and latest.get("status") != "withdrawn"):
                records[identity] = {"status": "public", "checked_at": attempted}
        store.set_meta("espn_portrait_visibility_v1", {"records": records,
            "updated_at": max(visibility.get("updated_at") or "", attempted)})
    directory = {"photos": {identity: value for identity, value in photos.items()
                           if identity in allowed and identity not in revoked},
                 "covered_ids": sorted(covered & allowed), "revoked_ids": sorted(revoked), "last_attempt": attempted,
                 "fetched_at": attempted if successes else previous.get("fetched_at"),
                 "status": "partial" if successes and errors else "completed" if successes else "unavailable"}
    store.set_meta(key, directory)
    _apply_espn_directory(snapshot, _espn_directory_with_revocations(store, directory, player_ids=allowed), selected=selected)
    return {"attempted_count": len(representatives), "errors": list(dict.fromkeys(errors))}


def _apply_directory(snapshot, directory):
    team_id = snapshot.get("team_id")
    if team_id not in DIRECTORIES:
        return snapshot
    cards = directory.get("cards", [])
    roster = snapshot.get("roster", [])
    by_name = {}
    for card in cards:
        by_name.setdefault(card["normalized_name"], []).append(card)
    counts = {}
    for player in roster:
        key = normalized_name(player.get("name"))
        counts[key] = counts.get(key, 0) + 1
    for player in roster:
        if player.get("photo_url") and player.get("photo_match") not in REFRESHABLE_PHOTO_MATCHES:
            continue
        # Recheck old official images against the current checked directory;
        # a changed/ambiguous shirt identity must not keep its previous photo.
        if cards and player.get("photo_match") == "exact_name_and_number":
            for field in ("photo_source_url", "photo_updated_at", "photo_match"):
                player.pop(field, None)
            player.update(photo_url="", photo_credit="")
        key = normalized_name(player.get("name"))
        matches = by_name.get(key, [])
        # Reject duplicate full names before considering a jersey number; a
        # shirt-number change is also a mismatch, never a reason to guess.
        if not key or counts.get(key) != 1 or len(matches) != 1 or not player.get("number"):
            continue
        card = matches[0]
        if str(player["number"]) != card["number"]:
            continue
        for field in PLAYER_PHOTO_FIELDS:
            player.pop(field, None)
        player.update(photo_url=card["photo_url"], photo_credit=DIRECTORIES[team_id]["credit"],
                      photo_source_url=card["source_url"], photo_updated_at=directory.get("fetched_at"),
                      photo_match="exact_name_and_number")
    return snapshot


async def enrich_player_photos(store, snapshot, *, player_ids=None, force=False, order=None, deadline=None,
                               on_progress=None):
    """Return a copied snapshot and bounded progress, without changing roster facts.

    ``order`` is a caller-supplied priority over the same roster — the model's
    answer to "which twelve first". It selects *who to ask about* and nothing
    else: the photo fields on a row are still written only by ``_apply_photo``
    from a source that proved the identity. ``deadline`` is a monotonic time
    within which this pass must finish, used to size the batch.

    ``on_progress`` is called with the working snapshot after each player is
    resolved. The pass runs under a wall-clock bound and the caller decides what
    to keep, so a pass that is cancelled partway through would otherwise lose
    every photo it had already checked — the work happened, the network was
    paid for, and only the write was missing.
    """
    result = copy.deepcopy(snapshot)
    roster = result.get("roster", [])
    if not isinstance(roster, list) or len(roster) > 120:
        raise IngestionError("球员照片需要已核对的球队阵容。")
    identities = [str(row.get("provider_id") or "") for row in roster]
    if any(not re.fullmatch(r"[0-9]{1,10}", identity) for identity in identities):
        raise IngestionError("球员照片需要可核对的球员编号。")
    if len(set(identities)) != len(identities):
        raise IngestionError("阵容球员编号重复，暂不匹配照片。")
    if player_ids is not None:
        if not isinstance(player_ids, (list, tuple)) or not 1 <= len(player_ids) <= 120:
            raise IngestionError("请选择阵容内需要补充照片的球员。")
        selection = {str(value) for value in player_ids}
        allowed = {str(row[key]) for row in roster for key in ("id", "provider_id") if row.get(key)}
        if selection - allowed:
            raise IngestionError("只能补充当前已核对阵容内的球员照片。")
        selected = [row for row in roster if str(row.get("id")) in selection or str(row.get("provider_id")) in selection]
    else:
        selected = roster
    if order is not None:
        selected = _ordered_selection(selected, order, roster)
    before = {row["provider_id"]: tuple(row.get(key) for key in PLAYER_PHOTO_FIELDS) for row in selected}
    # A whole-team manual retry resumes missing work. Only an explicit player
    # selection asks to revalidate a still-current checked photo.
    _recheck_espn_photos(selected)
    _recheck_community_photos(store, selected, result.get("team_id"))
    _recheck_wikidata_photos(store, selected)
    _recheck_wikimedia_photos(store, roster, result.get("team_id"))
    espn = await _enrich_espn_directory(store, result, selected, force=force)
    # Match against the entire roster so selecting one of two same-named players
    # cannot bypass the directory's ambiguity guard. Publish only selected photos.
    if selected and result.get("team_id") in DIRECTORIES:
        official = await enrich_portraits(store, copy.deepcopy(result), force=force)
        official_players = {row["provider_id"]: row for row in official["roster"]}
        for row in selected:
            source = official_players[row["provider_id"]]
            for field in PLAYER_PHOTO_FIELDS:
                if field in source:
                    row[field] = source[field]
                else:
                    row.pop(field, None)
    snapshot, summary = await _enrich_wikimedia_photos(store, result, selected, before, force=force,
                                                      explicit_selection=player_ids is not None or order is not None,
                                                      deadline=deadline, on_progress=on_progress)
    # A selected lookup can discover a Commons withdrawal shared by an
    # unselected old portrait. Clear it before returning the full snapshot.
    _recheck_wikimedia_photos(store, snapshot["roster"], snapshot.get("team_id"))
    summary["available_count"] = sum(bool(row.get("photo_url")) for row in snapshot["roster"])
    summary["selected_available_count"] = sum(bool(row.get("photo_url")) for row in selected)
    summary["missing_count"] = len(selected) - summary["selected_available_count"]
    summary["updated_count"] = sum(tuple(row.get(key) for key in PLAYER_PHOTO_FIELDS) != before[row["provider_id"]]
                                   for row in selected)
    if summary["status"] == "completed" and summary["missing_count"]:
        summary["status"] = "partial" if summary["selected_available_count"] else "missing"
    summary.update(espn_attempted_count=espn["attempted_count"], espn_errors=espn["errors"])
    return snapshot, summary


async def _enrich_wikimedia_photos(store, snapshot, selected, before, *, force, explicit_selection, deadline=None,
                                   on_progress=None):
    """Fill portraits from the community database first, then the bound sources.

    The order here is the acquisition order and it is measured, not preferred.
    The community full-name query runs first because it answers for most of a
    squad: across 107 players at four clubs it matched 81-90% with a birth date
    that agreed to the day, one paced request each, and it produced no
    wrong-person matches. Wikidata (11%, about three minutes per club) and
    Wikimedia's verified-club page stay behind it for whoever is still without
    a picture.

    What made this source look unusable, and why it is back, is worth keeping
    straight. The 2026-10-14 measurement that retired it was of the *surname*
    fallback and of ``lookup_all_players``. Both are genuinely bad: a surname
    query is a different question to the same endpoint and the ranking answers
    it with a stranger (``Saka`` ranks ``n'Sakala`` first), and the roster
    endpoint returns ten alphabetical rows containing coaches and players
    contracted elsewhere. A full-name query is neither. The endpoint ranks, so
    it returns one record, and an agreeing birth date is what confirms it —
    checked before any image is considered.

    A weaker source still cannot replace a stronger one; that arbitration is in
    ``_apply_photo`` and it is the only place it happens.
    """
    from .community_portraits import cached_portrait as cached_community, resolve_portrait as resolve_community
    from .wikimedia_portraits import cached_portrait as cached_wikimedia, resolve_portrait
    from .wikidata_portraits import MATCH_ESPN_PROVIDER_ID, cached_portrait as cached_wikidata, resolve_portrait as resolve_wikidata

    roster = snapshot["roster"]
    team_id = snapshot["team_id"]
    # Refusals where a source did answer, kept for the pass summary. See
    # ``_record_kept_source`` for why these are counted rather than dropped.
    kept_source = {}
    # The batch is sized against the time this pass actually has. A fixed
    # ceiling is what produced truncated batches: the phase budget expires
    # mid-player and the whole pass reports as interrupted, which reads as
    # missing photos.
    remaining_seconds = None if deadline is None else max(0.0, deadline - time.monotonic())
    community_limit = batch_limit(remaining_seconds, ceiling=COMMUNITY_PAGE_LIMIT)
    wikimedia_limit = batch_limit(remaining_seconds)
    cursor_key = f"wikimedia_cursor_v1_{team_id}_{snapshot.get('season_id', '')}"
    offset = 0 if explicit_selection or not selected else int(store.get_meta(cursor_key, {}).get("next", 0)) % len(selected)
    ordered = selected[offset:] + selected[:offset]
    cached_count = 0
    errors = {}
    wikimedia_failures = {}
    held_photo_failures = set()

    def checked_community(row):
        cached = cached_community(store, row, team_id)
        return bool(cached and cached.get("status") == "completed")

    # --- community database: the full-name query, first because it answers most
    # often and cheapest. The cursor advances by batch so an interrupted pass
    # resumes rather than restarting at the same players.
    community_cursor_key = f"community_cursor_v1_{team_id}_{snapshot.get('season_id', '')}"
    community_offset = (0 if explicit_selection or not selected else
                        int(store.get_meta(community_cursor_key, {}).get("next", 0)) % len(selected))
    community_ordered = selected[community_offset:] + selected[:community_offset]
    community_attempted = 0
    community_unavailable = False
    community_processed = []
    for row in community_ordered:
        if community_attempted >= community_limit:
            break
        # A row already holding a stronger picture is not asked about again —
        # a club's own directory or a licence-checked Commons file outranks this
        # source, so a request spent here could not be used.
        if row.get("photo_url") and photo_source_rank(row.get("photo_match")) is not None \
                and photo_source_rank(row.get("photo_match")) < photo_source_rank("community_name_and_birthdate"):
            continue
        refreshing = force and explicit_selection and row.get("photo_match") in COMMUNITY_PHOTO_MATCHES
        cached = None if refreshing else cached_community(store, row, team_id)
        if cached:
            photo = cached
        else:
            if deadline is not None and deadline - time.monotonic() < 12:
                break
            community_attempted += 1
            photo = await resolve_community(store, row, team_id, force=refreshing)
        community_processed.append(row)
        if photo.get("status") == "completed" and photo.get("photo_url"):
            if not _apply_photo(row, photo):
                _record_kept_source(kept_source, row, photo)
        elif photo.get("status") == "unavailable" and not cached:
            community_unavailable = True
            reason = ("社区球员库照片来源："
                      + (photo.get("photo_reason") or "社区球员库暂时不可达，稍后自动重试。"))
            row["photo_reason"] = reason
            row["retryable"] = bool(photo.get("retryable", True))
            row["reason_code"] = photo.get("reason_code") or "source_unavailable"
            errors[row["provider_id"]] = f"球员 {row['provider_id']}：{reason}"
            if on_progress:
                on_progress(snapshot)
            # An outage is not this player's answer, so the batch stops instead
            # of spending the remaining paced requests against a closed door.
            break
        elif photo.get("status") in {"missing", "ambiguous"}:
            # A durable verdict from this source. Written on the row so the
            # photo is not left looking merely un-attempted, and so a namesake
            # block reaches the acceptance report.
            _apply_source_verdict(row, photo)
        if on_progress and photo.get("status") == "completed":
            on_progress(snapshot)
    if not explicit_selection and community_processed:
        last = selected.index(community_processed[-1])
        store.set_meta(community_cursor_key, {"next": (last + 1) % len(selected), "updated_at": now()})

    def checked_wikimedia(row):
        cached = cached_wikimedia(store, row, team_id)
        return bool(cached and cached.get("status") == "completed" and all(cached.get(field) for field in
                    ("photo_url", "photo_credit", "photo_source_url", "photo_file_page", "photo_license")))

    def force_player(row):
        return force and (explicit_selection or not (row.get("photo_url") and checked_wikimedia(row)))

    # Id-anchored Commons portraits do not use the free name search. Cached    # answers are free; fresh lookups share the batch cap so one pass cannot
    # walk the whole squad at Wikimedia's pace. A miss is not written onto the
    # row — the verified-club page still gets to answer. A transport failure is
    # written, because it is a fact about this attempt rather than a verdict
    # that the player has no portrait.
    wikidata_cursor_key = f"wikidata_cursor_v1_{team_id}_{snapshot.get('season_id', '')}"
    wikidata_offset = (0 if explicit_selection or not selected else
                       int(store.get_meta(wikidata_cursor_key, {}).get("next", 0)) % len(selected))
    wikidata_ordered = selected[wikidata_offset:] + selected[:wikidata_offset]
    wikidata_attempted = 0
    wikidata_unavailable = False
    wikidata_processed = []
    wikidata_candidates = [row for row in wikidata_ordered
                           if not row.get("photo_url") or (force and explicit_selection
                                                           and row.get("photo_match") == MATCH_ESPN_PROVIDER_ID)]
    for row in wikidata_candidates:
        if wikidata_attempted >= community_limit:
            break
        refreshing = force and explicit_selection and row.get("photo_match") == MATCH_ESPN_PROVIDER_ID
        cached_id = None if refreshing else cached_wikidata(store, row)
        if cached_id:
            photo = cached_id
        else:
            if deadline is not None and deadline - time.monotonic() < 12:
                break
            wikidata_attempted += 1
            photo = await resolve_wikidata(store, row, force=refreshing)
        wikidata_processed.append(row)
        if photo.get("status") == "completed" and photo.get("photo_url"):
            _apply_photo(row, photo)
        elif photo.get("status") == "unavailable" and not cached_id:
            wikidata_unavailable = True
            reason = "维基数据照片来源：" + (photo.get("photo_reason") or "维基数据暂时不可达，稍后自动重试。")
            row["photo_reason"] = reason
            row["retryable"] = bool(photo.get("retryable", True))
            row["reason_code"] = photo.get("reason_code") or "source_unavailable"
            errors[row["provider_id"]] = f"球员 {row['provider_id']}：{reason}"
            if on_progress:
                on_progress(snapshot)
            break
        if on_progress and photo.get("status") == "completed":
            on_progress(snapshot)
    if not explicit_selection and wikidata_processed:
        last = selected.index(wikidata_processed[-1])
        store.set_meta(wikidata_cursor_key, {"next": (last + 1) % len(selected), "updated_at": now()})

    # Wikimedia looks at players still without a picture after the community and
    # id-anchored passes. Reusing its checked cache first keeps an outage of the
    # other sources from sending the whole squad here.
    for row in ordered:
        _apply_cached_reading(store, row, team_id)
        # An explicit forced selection is the caller saying "check these players
        # again", so nothing on the row may short-circuit it. The old expression
        # relied on `and` binding tighter than `or`, which meant an unrelated
        # `photo_url` test could still skip the player the caller had named.
        if force and explicit_selection:
            pass
        elif row.get("photo_url") and row.get("photo_match") != "wikimedia_verified_club":
            continue
        cached = cached_wikimedia(store, row, team_id)
        if not cached:
            continue
        cached_count += 1
        if cached.get("status") == "completed" and all(cached.get(field) for field in
                ("photo_url", "photo_credit", "photo_source_url", "photo_file_page", "photo_license")):
            if not _apply_photo(row, cached):
                _record_kept_source(kept_source, row, cached)
        else:
            if row.get("photo_match") == "wikimedia_verified_club":
                for field in PLAYER_PHOTO_FIELDS:
                    row.pop(field, None)
                row["photo_url"] = ""
                row["photo_credit"] = ""
            row["photo_reason"] = cached.get("photo_reason") or "球员照片未通过身份与许可核验。"
            row["retryable"] = bool(cached.get("retryable"))
            row["reason_code"] = cached.get("reason_code") or "rejected"
            if cached.get("status") == "unavailable":
                wikimedia_failures[row["provider_id"]] = cached
                errors[row["provider_id"]] = f"球员 {row['provider_id']}：维基媒体照片来源：{row['photo_reason']}"
    candidates = [row for row in ordered if (not row.get("photo_url") or
                   row.get("photo_match") == "wikimedia_verified_club" and
                   (force_player(row) or _elapsed(row.get("photo_updated_at")) >= CACHE_SECONDS))
                  and (force_player(row) or not cached_wikimedia(store, row, team_id))]
    requested, responses = [], []
    for row in candidates[:wikimedia_limit]:
        photo = (await resolve_portrait(store, row, team_id, force=True) if force_player(row)
                 else await resolve_portrait(store, row, team_id))
        requested.append(row)
        responses.append(photo)
        _apply_cached_reading(store, row, team_id)
        if photo.get("status") == "completed" and all(photo.get(k) for k in
                ("photo_url", "photo_credit", "photo_source_url", "photo_file_page", "photo_license")):
            _apply_photo(row, photo)
        elif row.get("photo_match") == "wikimedia_verified_club":
            if photo.get("status") != "unavailable" or not checked_wikimedia(row):
                for field in PLAYER_PHOTO_FIELDS:
                    row.pop(field, None)
                row["photo_url"] = ""
                row["photo_credit"] = ""
            row["photo_reason"] = photo.get("photo_reason") or "球员照片未通过身份与许可核验。"
            row["retryable"] = bool(photo.get("retryable"))
            row["reason_code"] = photo.get("reason_code") or "rejected"
        elif not row.get("photo_url"):
            # The community source may already have explained this player with
            # its own wording and its own reason code. Both sources were asked,
            # so both failures are kept in the message, each named after the
            # source that produced it, and the code stays with whichever source
            # was consulted first — the row must not end up describing one
            # outage while a different one is what the reader has to act on.
            incoming = "维基媒体照片来源：" + (photo.get("photo_reason") or "球员照片未通过身份与许可核验。")
            existing = row.get("photo_reason")
            row["photo_reason"] = f"{existing}；{incoming}" if existing and incoming not in existing else (existing or incoming)
            row.setdefault("retryable", bool(photo.get("retryable")))
            row.setdefault("reason_code", photo.get("reason_code") or "rejected")
        if photo.get("status") == "unavailable":
            wikimedia_failures[row["provider_id"]] = photo
            # Both sources can be down for the same player, and both facts
            # belong in the message. Overwriting would report one outage as if
            # the other had not happened, which is exactly the "a source that
            # answered nothing looks like a source that was never asked"
            # confusion the read path already refuses elsewhere.
            message = f"球员 {row['provider_id']}：维基媒体照片来源：{photo.get('photo_reason') or '照片来源暂不可用。'}"
            previous = errors.get(row["provider_id"])
            errors[row["provider_id"]] = f"{previous}；{message}" if previous else message
            # The reason *code* stays with the source whose limit stopped the
            # batch, because that is the one the reader acts on. The second
            # source's wording is added to the message rather than replacing it.
            row.setdefault("reason_code", photo.get("reason_code") or "unavailable")
            row.setdefault("retryable", bool(photo.get("retryable", True)))
            if row.get("photo_url") and row.get("photo_match") == "wikimedia_verified_club":
                held_photo_failures.add(row["provider_id"])
            # Bank this row before giving up on the source. A transport failure
            # says nothing about the remaining players, but the coordinator
            # caches a host failure for FAILURE_COOLDOWN_SECONDS, so every
            # remaining player would be answered from that cached error without
            # touching the network. Stopping here is therefore not what throws
            # work away — carrying on would only turn one real fault into N
            # reported ones. What preserves the work is this write.
            if on_progress:
                on_progress(snapshot)
            break
        if on_progress:
            on_progress(snapshot)
    if not explicit_selection and requested:
        last = selected.index(requested[-1])
        store.set_meta(cursor_key, {"next": (last + 1) % len(selected), "updated_at": now()})
    errors = [message for identity, message in errors.items()
              if identity in held_photo_failures or not next(row for row in selected if row["provider_id"] == identity).get("photo_url")]
    # A namesake verdict already in the community cache is a fact about this
    # roster entry. The default pass does not ask that publisher again, but it
    # still has to show the block so the planner does not treat the row as an
    # ordinary miss. It does not attach the old name-search photo.
    from .community_portraits import cached_portrait as cached_community
    for row in selected:
        if row.get("photo_url"):
            continue
        cached_name = cached_community(store, row, team_id)
        if not cached_name or not cached_name.get("namesake_blocked"):
            continue
        _copy_namesake(row, cached_name)
        row.setdefault("reason_code", cached_name.get("reason_code") or "community_ambiguous")
        row.setdefault("retryable", False)
        if not row.get("photo_reason"):
            row["photo_reason"] = cached_name.get("photo_reason") or "社区球员库只返回了一名同名球员。"
    for row in selected:
        if not row.get("photo_url") and not row.get("photo_reason"):
            row.update(photo_reason="照片来源尚未给出结论，下次更新会继续核对。",
                       retryable=True, reason_code="not_yet_resolved")
    available = sum(bool(row.get("photo_url")) for row in roster)
    selected_available = sum(bool(row.get("photo_url")) for row in selected)
    wikimedia_remaining = sum(not row.get("photo_url") for row in candidates[len(requested):])
    wikidata_remaining = sum(not row.get("photo_url") for row in wikidata_candidates[len(wikidata_processed):])
    # ``and`` binds tighter than ``or``. Parentheses keep an unavailable source
    # from reading as "the other source still has a batch ready".
    wikimedia_settled = any(photo.get("status") in {"completed", "missing", "rejected"} for photo in responses)
    wikimedia_down = any(photo.get("status") == "unavailable" for photo in responses)
    continuation_ready = bool(
        (wikidata_remaining and wikidata_processed and not wikidata_unavailable)
        or (wikimedia_remaining and wikimedia_settled and not wikimedia_down))
    status = ("completed" if selected_available == len(selected) and not errors else
              "partial" if selected_available or continuation_ready else "failed" if errors else "missing")
    return snapshot, _merge_kept_source({"status": status,
        "updated_count": sum(tuple(row.get(key) for key in PLAYER_PHOTO_FIELDS) != before[row["provider_id"]]
                             for row in selected),
        "available_count": available, "total_count": len(roster), "selected_count": len(selected),
        "selected_available_count": selected_available, "missing_count": len(selected) - selected_available,
        "attempted_count": len(responses), "cached_count": cached_count,
        # A fallback success finishes this player's photo work too: do not
        # report them as still queued in the preferred source's old batch.
        "remaining_count": wikimedia_remaining,
        # The community counters are real again. They were pinned to zero while
        # the name search was retired, with a comment saying the zero was for
        # older readers — which is the state this pass has left behind.
        "community_attempted_count": community_attempted,
        "wikidata_espn_attempted_count": wikidata_attempted,
        "wikidata_espn_unavailable": wikidata_unavailable,
        "wikidata_remaining_count": wikidata_remaining,
        "community_unavailable": community_unavailable,
        "community_remaining_count": max(0, len(selected) - community_attempted),
        "continuation_ready": continuation_ready,
        "errors": errors, "updated_at": now()}, kept_source)
