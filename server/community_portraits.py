"""Community portrait lookup with name + nationality identity checks.

TheSportsDB publishes a free JSON API with player portraits. Its `strTeam`
field is not reliable — a squad-mate of the same nationality can be listed
under a different club than the one the roster came from, and a transfer can
leave a stale club for weeks — so club membership is never used to accept a
photo. A portrait is accepted only when the searched name resolves to exactly
one record AND that record's nationality agrees with the checked roster entry.
Two independent facts must line up before an unlicensed image is shown.

Wikimedia remains the preferred source: it carries a verified free licence.
This provider runs afterwards and only fills players still without a photo.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import quote, urlsplit

from .ingestion import IngestionError, fetch_document, now, published_time, source_error_details
from .research_transport import blocking_request, shared_request

HOST = "www.thesportsdb.com"
API_PREFIX = "/api/v1/json/3/"
ACCEPTED_HOSTS = frozenset({HOST, "thesportsdb.com"})
IMAGE_HOSTS = frozenset({"r2.thesportsdb.com", "www.thesportsdb.com"})
MAX_JSON_BYTES = 512 * 1024
SUCCESS_SECONDS = 7 * 86400
# A missing name and an ambiguous name are durable answers about this roster
# entry; a throttle or transport fault is not, so it expires quickly.
RETRY_SECONDS = 900
# The free tier throttles bursts. Space requests out and retry a 429 once.
MIN_INTERVAL_SECONDS = 1.1
RETRY_BACKOFF_SECONDS = (6.0, 0.0)
PHOTO_FIELDS = ("photo_url", "photo_credit", "photo_source_url", "photo_file_page",
                "photo_license", "photo_restrictions", "photo_match", "photo_reason",
                "retryable", "reason_code")

# The publisher's own spelling varies ("The Netherlands" vs "Netherlands"), so
# compare on a normalised form that drops a leading article.
_LEADING_ARTICLE = re.compile(r"^(?:the|la|le|el|die|der|das)\s+")


def normalized(value):
    text = "".join(c for c in unicodedata.normalize("NFKD", str(value or "")).casefold()
                   if not unicodedata.combining(c))
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


def _country_key(value):
    return _LEADING_ARTICLE.sub("", normalized(value))


def _roster_country(nationality):
    """Normalise a roster nationality to compare against the publisher's.

    The roster stores the Chinese country name the team tooling already
    resolves, while the publisher returns an English one, so both are mapped
    into the same vocabulary. A value already in either form is accepted so a
    stored profile is not silently unmatched.
    """
    value = str(nationality or "").strip()
    if not value:
        return set()
    if re.search(r"[㐀-鿿]", value):
        return {_country_key(value)}
    from .team_center import COUNTRIES
    english = _country_key(value)
    chinese = {_country_key(zh) for en, zh in COUNTRIES.items() if _country_key(en) == english}
    return chinese or {english}


def _publisher_country(value):
    """Map the publisher's English country name into the roster vocabulary."""
    text = str(value or "").strip()
    if not text:
        return set()
    if re.search(r"[㐀-鿿]", text):
        return {_country_key(text)}
    from .team_center import COUNTRIES
    english = _country_key(text)
    chinese = {_country_key(zh) for en, zh in COUNTRIES.items() if _country_key(en) == english}
    return chinese or {english}


def _age_seconds(value):
    parsed = published_time(value)
    return (datetime.now(timezone.utc) - datetime.fromisoformat(parsed)).total_seconds() if parsed else float("inf")


def portrait_key(player, team_id):
    """Identity for the cache: the roster entry, not the publisher's record."""
    identity = f"{team_id}|{normalized(player.get('name'))}|{player.get('birth_date') or ''}|{player.get('number') or ''}"
    return "community_portrait_v1_" + sha256(identity.encode("utf-8")).hexdigest()[:24]


def cached_portrait(store, player, team_id):
    cached = store.get_meta(portrait_key(player, team_id), {})
    if cached.get("status") in {"completed", "missing", "ambiguous"} and _age_seconds(cached.get("fetched_at")) < SUCCESS_SECONDS:
        return cached
    if cached.get("status") == "unavailable" and _age_seconds(cached.get("last_attempt")) < RETRY_SECONDS:
        return cached
    return None


def _safe_image_url(value):
    """Accept only the publisher's own https image paths."""
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if (parts.scheme != "https" or parts.hostname not in IMAGE_HOSTS or parts.username or parts.password
            or parts.port not in {None, 443} or parts.fragment
            or not parts.path.startswith("/images/media/player/")
            or not parts.path.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))):
        return ""
    return value


async def _pace(store):
    """One paced request at a time; the free tier throttles bursts."""
    if not hasattr(store, "_community_last_request"):
        store._community_last_request = 0.0
    wait = MIN_INTERVAL_SECONDS - (time.monotonic() - store._community_last_request)
    if wait > 0:
        await asyncio.sleep(wait)
    store._community_last_request = time.monotonic()


async def _search(store, name):
    url = f"https://{HOST}{API_PREFIX}searchplayers.php?p={quote(name)}"

    async def factory():
        body, final_url, content_type = await blocking_request(fetch_document, url, timeout=22,
            allowed_hosts=ACCEPTED_HOSTS, max_body=MAX_JSON_BYTES,
            content_types=("application/json", "text/json", "text/plain", "text/html"))
        parts = urlsplit(final_url)
        if (parts.scheme != "https" or parts.hostname not in ACCEPTED_HOSTS or parts.username
                or parts.password or parts.port not in {None, 443}
                or not parts.path.startswith(API_PREFIX.rstrip("/")) or parts.fragment
                or len(body) > MAX_JSON_BYTES or "json" not in content_type.lower()):
            raise IngestionError("球员照片社区来源响应不完整，未采用照片。")
        try:
            payload = json.loads(body)
        except (UnicodeError, ValueError, RecursionError):
            raise IngestionError("球员照片社区来源资料无法读取，未采用照片。") from None
        if not isinstance(payload, dict):
            raise IngestionError("球员照片社区来源返回了无法识别的格式。")
        return payload

    last = None
    for attempt, wait in enumerate(RETRY_BACKOFF_SECONDS):
        await _pace(store)
        try:
            return await shared_request(store, "community:" + url, HOST, factory, force=attempt > 0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last = exc
            if source_error_details(exc)["code"] != "http_rate_limited":
                raise
            if wait:
                await asyncio.sleep(wait)
    raise last


def _unique_match(payload, name, nationality):
    """Exactly one record, and its nationality must agree with the roster.

    A club match is deliberately not required and not accepted: the publisher's
    club field was observed naming a different club for a player whose club the
    roster states, so it carries no identity weight in either direction.
    """
    records = payload.get("player")
    if records is None:
        return None, "missing"
    if not isinstance(records, list) or not 1 <= len(records) <= 50:
        return None, "unavailable"
    wanted = normalized(name)
    exact = [row for row in records if isinstance(row, dict) and normalized(row.get("strPlayer")) == wanted]
    if not exact:
        return None, "missing"
    wanted = _roster_country(nationality)
    if len(exact) > 1:
        # A namesake is only separable by a second identity fact, and the only
        # one both sides state is the country the roster already holds. If that
        # leaves nothing or leaves two, no photo is used: falling back to the
        # first record would publish a stranger's face.
        if not wanted:
            return None, "ambiguous"
        narrowed = [row for row in exact if _publisher_country(row.get("strNationality")) & wanted]
        if len(narrowed) != 1:
            return None, "ambiguous"
        exact = narrowed
    record = exact[0]
    if record.get("strSport") not in {None, "Soccer"} or record.get("strStatus") not in {None, "Active"}:
        return None, "missing"
    if not wanted:
        # Without a roster nationality there is only one identity fact, which
        # is not enough to publish an unlicensed portrait.
        return None, "ambiguous"
    if not (_publisher_country(record.get("strNationality")) & wanted):
        return None, "ambiguous"
    return record, "completed"


async def resolve_portrait(store, player, team_id, *, force=False):
    """Return checked portrait metadata or an explicit non-photo reason."""
    name = player.get("name") if isinstance(player, dict) else ""
    if not isinstance(name, str) or not normalized(name):
        return {"status": "missing", "photo_reason": "缺少可核对的球员姓名。", "retryable": False,
                "reason_code": "missing_name"}
    if not _roster_country((player or {}).get("nationality")):
        # With no roster nationality there is only one identity fact, which is
        # not enough to publish an unlicensed portrait. Answer without a
        # request rather than spending a paced lookup that cannot be accepted.
        return {"status": "ambiguous", "photo_reason": "缺少可核对的国籍，无法与社区球员库交叉确认。",
                "retryable": False, "reason_code": "missing_nationality"}
    key = portrait_key(player, team_id)
    cached = None if force else cached_portrait(store, player, team_id)
    if cached:
        return cached
    attempted = now()
    try:
        payload = await _search(store, name)
        record, verdict = _unique_match(payload, name, player.get("nationality"))
        if verdict != "completed":
            result = {"status": verdict, "photo_reason": {
                "missing": "社区球员库里没有完整姓名匹配的现役球员。",
                "ambiguous": "社区球员库的同名记录无法用国籍核对到这名球员。",
                "unavailable": "社区球员库返回的资料无法识别。",
            }[verdict], "retryable": verdict == "unavailable",
                "reason_code": "community_" + verdict}
        else:
            image = _safe_image_url(record.get("strCutout")) or _safe_image_url(record.get("strThumb"))
            profile = f"https://{HOST}/player/{record.get('idPlayer')}"
            if not image:
                result = {"status": "missing", "photo_reason": "社区球员库没有这名球员的可展示照片。",
                          "retryable": False, "reason_code": "community_no_image"}
            else:
                result = {"status": "completed", "photo_url": image,
                          "photo_credit": "TheSportsDB 社区球员资料图片",
                          "photo_source_url": profile, "photo_file_page": profile,
                          "photo_license": "第三方社区数据库转载；未声明开放许可",
                          "photo_restrictions": "保留来源署名，不得作为开放许可素材再分发",
                          "photo_match": "community_name_and_nationality", "photo_reason": ""}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        details = source_error_details(exc)
        # A throttle or transport fault says nothing about this player, so it is
        # rechecked on the short backoff instead of becoming a long-lived verdict.
        result = {"status": "unavailable", "photo_reason": "社区球员库暂时不可达，稍后自动重试。",
                  "retryable": True, "reason_code": details["code"]}
    result.update(fetched_at=now(), last_attempt=attempted)
    store.set_meta(key, result)
    return result
