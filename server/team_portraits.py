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
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment

from .ingestion import IngestionError, now, published_time, source_error_details
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
PLAYER_PHOTO_FIELDS = ("photo_url", "photo_credit", "photo_source_url", "photo_updated_at", "photo_match",
                       "photo_file_page", "photo_license", "photo_restrictions", "photo_reason",
                       "retryable", "reason_code")
# A photo carrying one of these matches was checked against the roster's own
# name AND number, so a later refresh may re-derive or replace it.
REFRESHABLE_PHOTO_MATCHES = frozenset({"exact_name_and_number", "wikimedia_verified_club",
                                       "community_name_and_nationality"})


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
    """A new checked source replaces all metadata from the previous attempt."""
    for field in PLAYER_PHOTO_FIELDS:
        player.pop(field, None)
    player.update({field: photo[field] for field in PLAYER_PHOTO_FIELDS if field in photo})
    player["photo_updated_at"] = photo.get("fetched_at") or photo.get("photo_updated_at") or now()


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


def _recheck_community_photos(store, players, team_id):
    """A roster's old image cannot bypass the current cache identity check."""
    from .community_portraits import cached_portrait
    for player in players:
        if player.get("photo_match") != "community_name_and_nationality":
            continue
        cached = cached_portrait(store, player, team_id)
        if cached and cached.get("status") == "completed":
            _apply_photo(player, cached)
        else:
            for field in PLAYER_PHOTO_FIELDS:
                player.pop(field, None)
            player.update(photo_url="", photo_credit="",
                          photo_reason="照片身份校验已更新，需要重新核对社区照片。",
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
            if final_url.rstrip("/") != config["url"]:
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
    from .wikimedia_portraits import cached_portrait

    _recheck_espn_photos(snapshot.get("roster", []))
    _recheck_community_photos(store, snapshot.get("roster", []), snapshot.get("team_id"))
    _recheck_wikimedia_photos(store, snapshot.get("roster", []), snapshot.get("team_id"))
    snapshot = _apply_espn_directory(snapshot, _espn_directory_with_revocations(store,
        store.get_meta("espn_portraits_v1_" + snapshot.get("team_id", ""), {}),
        player_ids={str(row.get("provider_id") or "") for row in snapshot.get("roster", [])}))
    snapshot = _apply_directory(snapshot, store.get_meta("team_portraits_v1_" + snapshot.get("team_id", ""), {}))
    for player in snapshot.get("roster", []):
        _apply_cached_reading(store, player, snapshot.get("team_id"))
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


async def enrich_player_photos(store, snapshot, *, player_ids=None, force=False):
    """Return a copied snapshot and bounded progress, without changing roster facts."""
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
    before = {row["provider_id"]: tuple(row.get(key) for key in PLAYER_PHOTO_FIELDS) for row in selected}
    # A whole-team manual retry resumes missing work. Only an explicit player
    # selection asks to revalidate a still-current checked photo.
    _recheck_espn_photos(selected)
    _recheck_community_photos(store, selected, result.get("team_id"))
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
                                                      explicit_selection=player_ids is not None)
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


async def _enrich_wikimedia_photos(store, snapshot, selected, before, *, force, explicit_selection):
    """Advance one bounded squad batch, then fill the rest from the community
    database; official directory successes are retained throughout."""
    from .wikimedia_portraits import cached_portrait as cached_wikimedia, resolve_portrait
    from .community_portraits import cached_portrait as cached_community, resolve_portrait as resolve_community

    roster = snapshot["roster"]
    team_id = snapshot["team_id"]
    cursor_key = f"wikimedia_cursor_v1_{team_id}_{snapshot.get('season_id', '')}"
    offset = 0 if explicit_selection or not selected else int(store.get_meta(cursor_key, {}).get("next", 0)) % len(selected)
    ordered = selected[offset:] + selected[:offset]
    cached_count = 0
    errors = {}
    wikimedia_failures = {}
    held_photo_failures = set()
    def checked_wikimedia(row):
        cached = cached_wikimedia(store, row, team_id)
        return bool(cached and cached.get("status") == "completed" and all(cached.get(field) for field in
                    ("photo_url", "photo_credit", "photo_source_url", "photo_file_page", "photo_license")))

    def force_player(row):
        return force and (explicit_selection or not (row.get("photo_url") and checked_wikimedia(row)))

    for row in ordered:
        _apply_cached_reading(store, row, team_id)
        if force and explicit_selection or row.get("photo_url") and row.get("photo_match") != "wikimedia_verified_club":
            continue
        cached = cached_wikimedia(store, row, team_id)
        if not cached:
            continue
        cached_count += 1
        if cached.get("status") == "completed" and all(cached.get(field) for field in
                ("photo_url", "photo_credit", "photo_source_url", "photo_file_page", "photo_license")):
            _apply_photo(row, cached)
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
    # Reuse a checked independent result before spending the preferred-source
    # budget. It can remain useful throughout a Wikimedia outage.
    if not force or not explicit_selection:
        for row in ordered:
            if not row.get("photo_url"):
                cached = cached_community(store, row, team_id)
                if cached and cached.get("status") == "completed" and cached.get("photo_url"):
                    _apply_photo(row, cached)
                    errors.pop(row["provider_id"], None)
    candidates = [row for row in ordered if (not row.get("photo_url") or
                   force and explicit_selection and row.get("photo_match") == "community_name_and_nationality" or
                   row.get("photo_match") == "wikimedia_verified_club" and
                   (force_player(row) or _elapsed(row.get("photo_updated_at")) >= CACHE_SECONDS))
                  and (force_player(row) or not cached_wikimedia(store, row, team_id))]
    requested, responses = [], []
    for row in candidates[:PLAYER_PAGE_LIMIT]:
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
            row["photo_reason"] = photo.get("photo_reason") or "球员照片未通过身份与许可核验。"
            row["retryable"] = bool(photo.get("retryable"))
            row["reason_code"] = photo.get("reason_code") or "rejected"
        if photo.get("status") == "unavailable":
            wikimedia_failures[row["provider_id"]] = photo
            errors[row["provider_id"]] = f"球员 {row['provider_id']}：维基媒体照片来源：{photo.get('photo_reason') or '照片来源暂不可用。'}"
            if row.get("photo_url") and row.get("photo_match") == "wikimedia_verified_club":
                held_photo_failures.add(row["provider_id"])
            # A transport failure says nothing about the remaining players.
            # Stop this source for the pass and let an independent source try.
            break
    if not explicit_selection and requested:
        last = selected.index(requested[-1])
        store.set_meta(cursor_key, {"next": (last + 1) % len(selected), "updated_at": now()})
    # Full name + nationality are checked independently by the fallback. A
    # preferred-source outage is therefore no reason to block it indefinitely.
    community_cursor_key = f"community_cursor_v1_{team_id}_{snapshot.get('season_id', '')}"
    community_offset = (0 if explicit_selection or not selected else
                        int(store.get_meta(community_cursor_key, {}).get("next", 0)) % len(selected))
    community_ordered = selected[community_offset:] + selected[:community_offset]
    def community_settled(row):
        cached = cached_community(store, row, team_id)
        return bool(cached) and cached.get("status") in {"missing", "ambiguous"}

    community_rows = [row for row in community_ordered if (not row.get("photo_url") or
                      force and explicit_selection and row.get("photo_match") == "community_name_and_nationality")
                      and (force or not community_settled(row))]
    attempted = 0
    processed = []
    community_unavailable = False
    for row in community_rows[:COMMUNITY_PAGE_LIMIT]:
        processed.append(row)
        cached = None if force else cached_community(store, row, team_id)
        if cached:
            photo = cached
        else:
            attempted += 1
            photo = await resolve_community(store, row, team_id, force=force)
        if photo.get("status") == "completed" and photo.get("photo_url"):
            _apply_photo(row, photo)
            errors.pop(row["provider_id"], None)
            held_photo_failures.discard(row["provider_id"])
        elif photo.get("status") != "unavailable" and force and explicit_selection and row.get("photo_match") == "community_name_and_nationality":
            # A fresh identity/deletion verdict invalidates this source; a
            # transport failure alone does not invalidate a current proof.
            for field in PLAYER_PHOTO_FIELDS:
                row.pop(field, None)
            row.update(photo_url="", photo_credit="", photo_reason=photo.get("photo_reason") or "社区照片未通过身份核对。",
                       retryable=bool(photo.get("retryable")), reason_code=photo.get("reason_code") or "community_rejected")
        elif not row.get("photo_reason"):
            # The preferred source already explained this player; only speak up
            # when the row would otherwise show a placeholder with no reason.
            row["photo_reason"] = photo.get("photo_reason") or "球员照片未通过身份核对。"
            row["retryable"] = bool(photo.get("retryable"))
            row["reason_code"] = photo.get("reason_code") or "rejected"
        if photo.get("status") == "unavailable":
            community_unavailable = True
            reason = _community_failure(row, photo, wikimedia_failures.get(row["provider_id"]))
            errors[row["provider_id"]] = f"球员 {row['provider_id']}：{reason}"
            if row.get("photo_url"):
                held_photo_failures.add(row["provider_id"])
            if not cached:
                break
    if not explicit_selection and processed:
        last = selected.index(processed[-1])
        store.set_meta(community_cursor_key, {"next": (last + 1) % len(selected), "updated_at": now()})
    errors = [message for identity, message in errors.items()
              if identity in held_photo_failures or not next(row for row in selected if row["provider_id"] == identity).get("photo_url")]
    for row in selected:
        if not row.get("photo_url") and not row.get("photo_reason"):
            row.update(photo_reason="照片来源尚未给出结论，下次更新会继续核对。",
                       retryable=True, reason_code="not_yet_resolved")
    available = sum(bool(row.get("photo_url")) for row in roster)
    selected_available = sum(bool(row.get("photo_url")) for row in selected)
    wikimedia_remaining = sum(not row.get("photo_url") for row in candidates[len(requested):])
    community_remaining = sum(not row.get("photo_url") for row in community_rows[len(processed):])
    continuation_ready = bool(
        community_remaining and processed and not community_unavailable
        or wikimedia_remaining and any(photo.get("status") in {"completed", "missing", "rejected"} for photo in responses)
        and not any(photo.get("status") == "unavailable" for photo in responses))
    status = ("completed" if selected_available == len(selected) and not errors else
              "partial" if selected_available or continuation_ready else "failed" if errors else "missing")
    return snapshot, {"status": status,
        "updated_count": sum(tuple(row.get(key) for key in PLAYER_PHOTO_FIELDS) != before[row["provider_id"]]
                             for row in selected),
        "available_count": available, "total_count": len(roster), "selected_count": len(selected),
        "selected_available_count": selected_available, "missing_count": len(selected) - selected_available,
        "attempted_count": len(responses), "cached_count": cached_count,
        # A fallback success finishes this player's photo work too: do not
        # report them as still queued in the preferred source's old batch.
        "remaining_count": wikimedia_remaining,
        "community_attempted_count": attempted,
        "community_unavailable": community_unavailable,
        "community_remaining_count": community_remaining,
        "continuation_ready": continuation_ready,
        "errors": errors, "updated_at": now()}
