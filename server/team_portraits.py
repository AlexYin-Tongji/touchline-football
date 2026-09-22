"""Small official-directory portrait fallback; never search by a loose surname.

The Barcelona directory is one public HTML page. Images are accepted only from
explicit player cards on that page, uniquely matched to a verified ESPN squad by
full name and shirt number. This adds images, never squad membership or facts.
"""
from __future__ import annotations

import asyncio
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
            body, final_url, _ = await asyncio.wait_for(asyncio.to_thread(
                fetch_document, config["url"], allowed_hosts={config["host"]},
                max_body=2 * 1024 * 1024, content_types=("text/html", "xhtml")), timeout=22)
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
