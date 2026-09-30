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
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .ingestion import IngestionError, fetch_document, now, published_time

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
# The community database throttles bursts, so its requests are paced serially
# at about one per second, one player at a time. The bound must leave room in
# the 180s phase for the Wikimedia batch that runs before it (about 6-7s per
# player for up to PLAYER_PAGE_LIMIT players); the cursor carries the rest of
# the squad into the next round rather than overrunning the phase. Measured:
# a 29-player squad completes in ~32s when the data is there, but a squad whose
# names mostly miss costs a full paced lookup each and runs four times longer,
# which is why an unbounded loop is not safe.
COMMUNITY_PAGE_LIMIT = 40
PLAYER_PHOTO_FIELDS = ("photo_url", "photo_credit", "photo_source_url", "photo_updated_at", "photo_match",
                       "photo_file_page", "photo_license", "photo_restrictions", "photo_reason",
                       "retryable", "reason_code")
# A photo carrying one of these matches was checked against the roster's own
# name AND number, so a later refresh may re-derive or replace it.
REFRESHABLE_PHOTO_MATCHES = frozenset({"exact_name_and_number", "wikimedia_verified_club",
                                       "community_name_and_nationality"})


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
        if (parsed.scheme != "https" or parsed.hostname != config["host"] or parsed.username or parsed.password
                or parsed.port not in {None, 443}):
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


def _card_image(node):
    image = node.select_one("img.team-person__image") if node else None
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
            "image": _card_image(card),
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
        raise IngestionError("官网球员目录暂未提供可核对的头像。")
    return cards


async def portrait_directory(store, team_id):
    config = DIRECTORIES.get(team_id)
    if not config:
        return {}
    if not hasattr(store, "_portrait_locks"):
        store._portrait_locks = {}
    lock = store._portrait_locks.setdefault(team_id, asyncio.Lock())
    async with lock:
        key = "team_portraits_v1_" + team_id
        previous = store.get_meta(key, {})
        if (previous.get("cards") and _elapsed(previous.get("fetched_at")) < CACHE_SECONDS
                or _elapsed(previous.get("last_attempt")) < RETRY_SECONDS):
            return previous
        attempted = now()
        try:
            # Fixed host and one directory only; redirects remain on this host.
            body, final_url, _ = await _portrait_document(store, config["url"],
                allowed_hosts={config["host"]}, max_body=2 * 1024 * 1024)
            if final_url.rstrip("/") != config["url"]:
                raise IngestionError("官网球员目录地址已变化，暂不更新头像。")
            cards = parse_directory(body, team_id)
            result = {"team_id": team_id, "cards": cards, "source_url": config["url"],
                      "fetched_at": now(), "last_attempt": attempted, "status": "completed"}
        except asyncio.CancelledError:
            raise
        except Exception:
            # The last checked photos remain eligible only after matching this
            # refresh's confirmed player name AND number again.
            result = {**previous, "last_attempt": attempted, "status": "unavailable"}
        store.set_meta(key, result)
        return result


async def enrich_portraits(store, snapshot):
    team_id = snapshot.get("team_id")
    if team_id not in DIRECTORIES or not any(not player.get("photo_url") or player.get("photo_match") in
            REFRESHABLE_PHOTO_MATCHES for player in snapshot.get("roster", [])):
        return snapshot
    directory = await portrait_directory(store, team_id)
    return _apply_directory(snapshot, directory)


def apply_cached_portraits(store, snapshot):
    """Reuse checked cards without making a core roster fetch wait on HTML."""
    from .community_portraits import cached_portrait as cached_community
    from .wikimedia_portraits import PHOTO_FIELDS, cached_portrait

    snapshot = _apply_directory(snapshot, store.get_meta("team_portraits_v1_" + snapshot.get("team_id", ""), {}))
    for player in snapshot.get("roster", []):
        if player.get("photo_url"):
            continue
        cached = cached_portrait(store, player, snapshot.get("team_id"))
        if cached and cached.get("status") == "completed" and all(cached.get(field) for field in
                ("photo_url", "photo_credit", "photo_source_url", "photo_file_page", "photo_license")):
            player.update({field: cached[field] for field in PHOTO_FIELDS if field in cached})
            player["photo_updated_at"] = cached.get("fetched_at")
            continue
        community = cached_community(store, player, snapshot.get("team_id"))
        if community and community.get("status") == "completed" and community.get("photo_url"):
            player.update({field: community[field] for field in PHOTO_FIELDS if community.get(field)})
            player["photo_updated_at"] = community.get("fetched_at")
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
        if player.get("photo_url") and player.get("photo_match") not in {"exact_name_and_number", "wikimedia_verified_club"}:
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
        if player.get("photo_match") == "wikimedia_verified_club":
            for field in ("photo_file_page", "photo_license", "photo_restrictions", "photo_reason"):
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
    # Match against the entire roster so selecting one of two same-named players
    # cannot bypass the directory's ambiguity guard. Publish only selected photos.
    if selected and result.get("team_id") in DIRECTORIES:
        official = await enrich_portraits(store, copy.deepcopy(result))
        official_players = {row["provider_id"]: row for row in official["roster"]}
        for row in selected:
            source = official_players[row["provider_id"]]
            for field in PLAYER_PHOTO_FIELDS:
                if field in source:
                    row[field] = source[field]
                else:
                    row.pop(field, None)
    return await _enrich_wikimedia_photos(store, result, selected, before, force=force,
                                          explicit_selection=player_ids is not None)


async def _enrich_wikimedia_photos(store, snapshot, selected, before, *, force, explicit_selection):
    """Advance one bounded squad batch, then fill the rest from the community
    database; official directory successes are retained throughout."""
    from .wikimedia_portraits import PHOTO_FIELDS, cached_portrait as cached_wikimedia, resolve_portrait

    roster = snapshot["roster"]
    team_id = snapshot["team_id"]
    cursor_key = f"wikimedia_cursor_v1_{team_id}_{snapshot.get('season_id', '')}"
    offset = 0 if explicit_selection or not selected else int(store.get_meta(cursor_key, {}).get("next", 0)) % len(selected)
    ordered = selected[offset:] + selected[:offset]
    cached_count = 0
    errors = []
    for row in ordered:
        if force or row.get("photo_url") and row.get("photo_match") != "wikimedia_verified_club":
            continue
        cached = cached_wikimedia(store, row, team_id)
        if not cached:
            continue
        cached_count += 1
        if cached.get("status") == "completed" and all(cached.get(field) for field in
                ("photo_url", "photo_credit", "photo_source_url", "photo_file_page", "photo_license")):
            row.update({field: cached[field] for field in PHOTO_FIELDS if field in cached})
            row["photo_updated_at"] = cached.get("fetched_at")
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
                errors.append(f"球员 {row['provider_id']}：{row['photo_reason']}")
    candidates = [row for row in ordered if (not row.get("photo_url") or
                   row.get("photo_match") == "wikimedia_verified_club" and
                   (force or _elapsed(row.get("photo_updated_at")) >= CACHE_SECONDS))
                  and (force or not cached_wikimedia(store, row, team_id))]
    requested = candidates[:PLAYER_PAGE_LIMIT]
    responses = await asyncio.gather(*(resolve_portrait(store, row, team_id, force=True)
                                       if force else resolve_portrait(store, row, team_id) for row in requested))
    for row, photo in zip(requested, responses):
        if photo.get("status") == "completed" and all(photo.get(k) for k in
                ("photo_url", "photo_credit", "photo_source_url", "photo_file_page", "photo_license")):
            row.update({field: photo[field] for field in PHOTO_FIELDS if field in photo})
            row["photo_updated_at"] = photo.get("fetched_at") or now()
        elif row.get("photo_match") == "wikimedia_verified_club":
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
            errors.append(f"球员 {row['provider_id']}：{row.get('photo_reason', '照片来源暂不可用。')}")
    if not explicit_selection and requested:
        last = selected.index(requested[-1])
        store.set_meta(cursor_key, {"next": (last + 1) % len(selected), "updated_at": now()})
    # Wikimedia is preferred because it carries a checked free licence. Whatever
    # it cannot supply falls through to the community database, which needs two
    # agreeing identity facts (full name and nationality) before its unlicensed
    # portrait is shown. This is what actually fills a squad: a free-licensed
    # photo exists for only about a fifth of players.
    #
    # A *verdict* closes the question — this player has no photo Wikimedia will
    # accept — so the community source is asked instead. An *unanswered* pass
    # answers nothing, so the question stays open for the next one rather than
    # being handed to the community source immediately. Both cases matter: the
    # first is what left a player Wikimedia once filled with no fallback once its
    # cache expired, and the second is what would spend a paced community lookup
    # on every player in a squad during a Wikimedia outage.
    from .community_portraits import cached_portrait as cached_community, resolve_portrait as resolve_community
    def community_settled(row):
        cached = cached_community(store, row, team_id)
        return bool(cached) and cached.get("status") in {"missing", "ambiguous"}

    def wikimedia_decided(row):
        """Whether Wikimedia produced an answer about this player, or nothing yet.

        A missing cache entry means unanswered, and an `unavailable` entry means
        the lookup itself failed. Neither is a verdict about the player, so
        neither hands them to the community source in this pass. Everything else
        — a completed photo, or a refusal on identity or licence — is a verdict,
        and a refusal is exactly what the community source exists to cover.
        """
        cached = cached_wikimedia(store, row, team_id)
        return bool(cached) and cached.get("status") != "unavailable"

    community_rows = [row for row in ordered if not row.get("photo_url")
                      and (force or (wikimedia_decided(row) and not community_settled(row)))]
    attempted = 0
    for row in community_rows:
        if attempted >= COMMUNITY_PAGE_LIMIT and not explicit_selection:
            break
        cached = None if force else cached_community(store, row, team_id)
        if cached:
            photo = cached
        else:
            attempted += 1
            photo = await resolve_community(store, row, team_id, force=force)
        if photo.get("status") == "completed" and photo.get("photo_url"):
            row.update({field: photo[field] for field in PHOTO_FIELDS if photo.get(field)})
            row["photo_updated_at"] = photo.get("fetched_at") or now()
        elif not row.get("photo_reason"):
            # The preferred source already explained this player; only speak up
            # when the row would otherwise show a placeholder with no reason.
            row["photo_reason"] = photo.get("photo_reason") or "球员照片未通过身份核对。"
            row["retryable"] = bool(photo.get("retryable"))
            row["reason_code"] = photo.get("reason_code") or "rejected"
        if photo.get("status") == "unavailable":
            errors.append(f"球员 {row['provider_id']}：{row.get('photo_reason', '照片来源暂不可用。')}")
    available = sum(bool(row.get("photo_url")) for row in roster)
    selected_available = sum(bool(row.get("photo_url")) for row in selected)
    status = ("completed" if selected_available == len(selected) and not errors else
              "partial" if selected_available else "failed" if errors else "missing")
    return snapshot, {"status": status,
        "updated_count": sum(tuple(row.get(key) for key in PLAYER_PHOTO_FIELDS) != before[row["provider_id"]]
                             for row in selected),
        "available_count": available, "total_count": len(roster), "selected_count": len(selected),
        "selected_available_count": selected_available, "missing_count": len(selected) - selected_available,
        "attempted_count": len(responses), "cached_count": cached_count,
        "remaining_count": len(candidates) - len(requested), "errors": errors, "updated_at": now()}
