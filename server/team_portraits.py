"""Bounded source-backed portraits; never search by a loose surname.

The Barcelona directory is one public HTML page. Images are accepted only from
explicit player cards on that page, uniquely matched to a verified ESPN squad by
full name and shirt number. This adds images, never squad membership or facts.
Other clubs use an ESPN player page only after provider player and team IDs agree.
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
    "barcelona": {"url": "https://www.fcbarcelona.com/en/football/first-team/players",
                  "host": "www.fcbarcelona.com", "credit": "FC Barcelona 官方球员目录"},
}
CACHE_SECONDS = 86400
RETRY_SECONDS = 3600
PLAYER_PAGE_LIMIT = 8
PLAYER_PAGE_CONCURRENCY = 3
PLAYER_PAGE_MAX_BODY = 4 * 1024 * 1024
PLAYER_PHOTO_FIELDS = ("photo_url", "photo_credit", "photo_source_url", "photo_updated_at", "photo_match")


class _PortraitIdentityError(IngestionError):
    """An explicit mismatch must not revive an older provider photo from cache."""


async def _portrait_document(store, url, *, allowed_hosts, max_body):
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
                    allowed_hosts=allowed_hosts, max_body=max_body, content_types=("text/html", "xhtml")))
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
            if not parsed.path.startswith("/photo-resources/") or not parsed.path.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                return ""
        elif not parsed.path.startswith("/en/football/first-team/players/"):
            return ""
        return url
    except ValueError:
        return ""


def parse_directory(body, team_id):
    config = DIRECTORIES.get(team_id)
    if not config:
        return []
    soup = BeautifulSoup(body, "html.parser")
    cards = []
    for card in soup.select("a.team-person[href]"):
        # Coach and other team sections do not qualify as first-team players.
        profile = _public_url(card.get("href"), config)
        names = card.select(".team-person__first-name, .team-person__last-name")
        name = " ".join(node.get_text(" ", strip=True) for node in names).strip()
        number = card.select_one(".team-person__number")
        number = number.get_text(strip=True) if number else ""
        image = card.select_one("img.team-person__image")
        image_url = _public_url(image.get("data-image-src") or image.get("src"), config, image=True) if image else ""
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
    if team_id not in DIRECTORIES or not any(not player.get("photo_url") or player.get("photo_match") == "exact_name_and_number" for player in snapshot.get("roster", [])):
        return snapshot
    directory = await portrait_directory(store, team_id)
    return _apply_directory(snapshot, directory)


def apply_cached_portraits(store, snapshot):
    """Reuse checked cards without making a core roster fetch wait on HTML."""
    return _apply_directory(snapshot, store.get_meta("team_portraits_v1_" + snapshot.get("team_id", ""), {}))


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
        if player.get("photo_url") and player.get("photo_match") != "exact_name_and_number":
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
        player.update(photo_url=card["photo_url"], photo_credit=DIRECTORIES[team_id]["credit"],
                      photo_source_url=card["source_url"], photo_updated_at=directory.get("fetched_at"),
                      photo_match="exact_name_and_number")
    return snapshot


def _provider_id(value):
    identity = str(value)
    if not re.fullmatch(r"[0-9]{1,10}", identity):
        raise IngestionError("球员照片需要可核对的 ESPN 球员和球队编号。")
    return identity


def _player_object(value):
    if not isinstance(value, dict):
        raise IngestionError("ESPN 球员网页缺少可核对的身份资料。")
    return value


def _espn_headshot_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == "https" and parsed.hostname in {"a.espncdn.com", "a1.espncdn.com", "a2.espncdn.com", "a3.espncdn.com", "a4.espncdn.com"}
                and not parsed.username and not parsed.password and parsed.port in {None, 443}
                and not parsed.fragment and parsed.path.startswith("/i/headshots/soccer/players/")
                and parsed.path.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))):
            return value
    except ValueError:
        pass
    return ""


def parse_player_portrait(body, provider_player_id, provider_team_id):
    """Read only the identified player's explicit headshot, never related media."""
    player_id, team_id = _provider_id(provider_player_id), _provider_id(provider_team_id)
    if isinstance(body, bytes):
        if len(body) > PLAYER_PAGE_MAX_BODY:
            raise IngestionError("ESPN 球员网页超过读取上限。")
        try:
            body = body.decode("utf-8")
        except UnicodeError:
            raise IngestionError("ESPN 球员网页编码无法识别。") from None
    if not isinstance(body, str) or len(body.encode("utf-8")) > PLAYER_PAGE_MAX_BODY:
        raise IngestionError("ESPN 球员网页超过读取上限。")
    markers = list(re.finditer(r"window(?:\[['\"]__espnfitt__['\"]\]|\.__espnfitt__)\s*=\s*", body))
    if len(markers) != 1:
        raise IngestionError("ESPN 球员网页缺少唯一的结构化资料。")
    try:
        payload, _ = json.JSONDecoder().raw_decode(body[markers[0].end():])
    except (ValueError, RecursionError):
        raise IngestionError("ESPN 球员网页结构化资料无法读取。") from None
    params = _player_object(_player_object(_player_object(payload).get("routing")).get("params"))
    page = _player_object(payload.get("page"))
    player = _player_object(_player_object(page.get("content")).get("player"))
    athlete = _player_object(_player_object(player.get("prtlCmnApiRsp")).get("athlete"))
    team = _player_object(athlete.get("team"))
    if (params.get("sport") != "soccer" or params.get("pageType") != "player"
            or str(params.get("id")) != player_id or str(player.get("playerId")) != player_id
            or player.get("sport", "soccer") != "soccer" or str(athlete.get("id")) != player_id
            or athlete.get("uid") != "s:600~a:" + player_id or str(team.get("id")) != team_id
            or team.get("uid", "s:600~t:" + team_id) != "s:600~t:" + team_id):
        raise _PortraitIdentityError("ESPN 球员网页的球员或所属球队编号不匹配，未采用照片。")
    photo = athlete.get("headshot")
    photo = _espn_headshot_url(photo.get("href")) if isinstance(photo, dict) else ""
    if not photo:
        header = player.get("plyrHdr")
        header = header.get("ath") if isinstance(header, dict) else None
        if isinstance(header, dict):
            if (any(header[key] != "s:600~t:" + team_id for key in ("tmUid", "tmUID") if key in header)
                    or "id" in header and str(header["id"]) != player_id
                    or "uid" in header and header["uid"] != "s:600~a:" + player_id):
                raise _PortraitIdentityError("ESPN 球员页眉身份不匹配，未采用照片。")
            supplied = header.get("headshot")
            photo = _espn_headshot_url(supplied.get("href") if isinstance(supplied, dict) else supplied)
    return {"photo_url": photo, "photo_credit": "ESPN 球员资料图片" if photo else "",
            "photo_source_url": f"https://www.espn.com/soccer/player/_/id/{player_id}" if photo else "",
            "photo_match": "provider_player_id" if photo else ""}


def _player_cache_key(team_id, player_id):
    return f"player_portrait_v1_{team_id}_{player_id}"


def _fresh_player_cache(previous, team_id, player_id):
    if not isinstance(previous, dict) or previous.get("provider_team_id") != team_id or previous.get("provider_player_id") != player_id:
        return False
    if previous.get("status") in {"completed", "missing"}:
        return _elapsed(previous.get("fetched_at")) < CACHE_SECONDS
    return previous.get("status") == "unavailable" and _elapsed(previous.get("last_attempt")) < RETRY_SECONDS


async def _fetch_player_portrait(store, team_id, player_id, *, force=False):
    key = _player_cache_key(team_id, player_id)
    lock = store._player_portrait_locks.setdefault(key, asyncio.Lock())
    async with lock:
        previous = store.get_meta(key, {})
        if not force and _fresh_player_cache(previous, team_id, player_id):
            return previous, True
        url = f"https://www.espn.com/soccer/player/_/id/{player_id}"
        attempted = now()
        try:
            body, final_url, content_type = await _portrait_document(store, url,
                allowed_hosts={"www.espn.com"}, max_body=PLAYER_PAGE_MAX_BODY)
            final = urlsplit(final_url)
            route = re.fullmatch(r"/soccer/player/_/id/([0-9]{1,10})(?:/[^/]+)?/?", final.path)
            if (final.scheme != "https" or final.hostname != "www.espn.com" or final.username or final.password
                    or final.port not in {None, 443} or final.query or final.fragment or not route
                    or route.group(1) != player_id):
                raise _PortraitIdentityError("ESPN 球员网页地址不匹配，未采用照片。")
            if not any(kind in content_type.lower() for kind in ("text/html", "xhtml")):
                raise IngestionError("ESPN 球员网页内容类型不匹配，未采用照片。")
            photo = parse_player_portrait(body, player_id, team_id)
            fetched = now()
            result = {**photo, "provider_team_id": team_id, "provider_player_id": player_id,
                      "fetched_at": fetched, "last_attempt": attempted,
                      "status": "completed" if photo["photo_url"] else "missing", "errors": []}
            if photo["photo_url"]:
                result["photo_updated_at"] = fetched
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Keep only a prior portrait whose exact provider identity was already
            # checked; raw page content and network exception details are not saved.
            identity_rejected = isinstance(exc, _PortraitIdentityError)
            prior = previous if not identity_rejected and isinstance(previous, dict) and previous.get("provider_team_id") == team_id and previous.get("provider_player_id") == player_id else {}
            result = {**prior, "provider_team_id": team_id, "provider_player_id": player_id,
                      "last_attempt": attempted, "status": "unavailable",
                      "identity_rejected": identity_rejected,
                      "errors": [str(exc) if isinstance(exc, IngestionError) else "ESPN 球员网页暂不可访问，已有照片保留。"]}
        store.set_meta(key, result)
        return result, False


async def enrich_player_photos(store, snapshot, provider_team_id, *, player_ids=None, force=False):
    """Return a copied snapshot and bounded progress, without changing roster facts."""
    team_id = _provider_id(provider_team_id)
    result = copy.deepcopy(snapshot)
    roster = result.get("roster", [])
    if not isinstance(roster, list) or len(roster) > 120:
        raise IngestionError("球员照片需要已核对的球队阵容。")
    identities = [_provider_id(row.get("provider_id")) for row in roster]
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
    if not hasattr(store, "_player_portrait_locks"):
        store._player_portrait_locks = {}
    candidates = [row for row in selected if not row.get("photo_url") or force and row.get("photo_match") == "provider_player_id"]
    requested, pending, fetched = [], 0, 0
    for row in candidates:
        identity = str(row["provider_id"])
        cached = not force and _fresh_player_cache(store.get_meta(_player_cache_key(team_id, identity), {}), team_id, identity)
        if not cached and fetched >= PLAYER_PAGE_LIMIT:
            pending += 1
            continue
        fetched += not cached
        requested.append(row)
    responses = await asyncio.gather(*(_fetch_player_portrait(store, team_id, str(row["provider_id"]), force=force) for row in requested))
    errors, cached_count = [], 0
    for row, (photo, cached) in zip(requested, responses):
        cached_count += cached
        errors.extend(f"球员 {row['provider_id']}：{error}" for error in photo.get("errors", []))
        if photo.get("identity_rejected") and row.get("photo_match") == "provider_player_id":
            for field in PLAYER_PHOTO_FIELDS:
                row.pop(field, None)
            row.update(photo_url="", photo_credit="")
            continue
        if (photo.get("status") != "unavailable" and _espn_headshot_url(photo.get("photo_url")) and photo.get("photo_match") == "provider_player_id"
                and photo.get("photo_source_url") == f"https://www.espn.com/soccer/player/_/id/{row['provider_id']}"):
            row.update({key: photo[key] for key in PLAYER_PHOTO_FIELDS if key in photo})
    available = sum(bool(row.get("photo_url")) for row in roster)
    selected_available = sum(bool(row.get("photo_url")) for row in selected)
    summary = {"status": "completed" if selected_available == len(selected) and not errors else "partial" if selected_available else "unavailable",
               "updated_count": sum(tuple(row.get(key) for key in PLAYER_PHOTO_FIELDS) != before[row["provider_id"]] for row in selected),
               "available_count": available, "total_count": len(roster), "selected_count": len(selected),
               "selected_available_count": selected_available, "missing_count": len(selected) - selected_available,
               "attempted_count": len(responses) - cached_count, "cached_count": cached_count,
               "remaining_count": pending, "errors": errors, "updated_at": now()}
    return result, summary
