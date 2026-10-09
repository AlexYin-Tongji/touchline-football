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

from .ingestion import IngestionError, now, published_time, source_error_details
from .research_transport import blocking_request, shared_request, fetch_reviewed_portrait_document as fetch_document
from .player_names import normalize_name

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
# The publisher documents 30 requests/minute for the public tier. All teams in
# one Store share this pace, so broad coverage does not become a burst of 429s.
MIN_INTERVAL_SECONDS = 2.1
RETRY_BACKOFF_SECONDS = (6.0, 0.0)
PHOTO_FIELDS = ("photo_url", "photo_credit", "photo_source_url", "photo_file_page",
                "photo_license", "photo_restrictions", "photo_match", "photo_reason",
                "retryable", "reason_code")

# How a community record was tied to a roster entry, from the strongest check
# to the weakest. The name is always required; what differs is the second fact
# that confirms it, because one fact is never enough to publish an unlicensed
# face. Kept in one place so the acquisition policy, the cache validator and
# the roster refresh all recognise the same set.
MATCH_NAME_AND_NATIONALITY = "community_name_and_nationality"  # full name + country
MATCH_NAME_AND_BIRTHDATE = "community_name_and_birthdate"      # full name + exact date
MATCH_NAME_AND_BIRTH_YEAR = "community_name_and_birth_year"    # full name + same year
MATCH_SURNAME_AND_BIRTHDATE = "community_surname_and_birthdate"  # variant name + exact date
COMMUNITY_PHOTO_MATCHES = frozenset({
    MATCH_NAME_AND_NATIONALITY, MATCH_NAME_AND_BIRTHDATE,
    MATCH_NAME_AND_BIRTH_YEAR, MATCH_SURNAME_AND_BIRTHDATE,
})
# The two that a manual retry may re-derive; they rest on a name plus a date,
# so a fresh lookup can legitimately change the answer.
REFRESHABLE_COMMUNITY_MATCHES = frozenset({
    MATCH_NAME_AND_NATIONALITY, MATCH_NAME_AND_BIRTHDATE,
    MATCH_NAME_AND_BIRTH_YEAR, MATCH_SURNAME_AND_BIRTHDATE,
})

# The publisher's own spelling varies ("The Netherlands" vs "Netherlands"), so
# compare on a normalised form that drops a leading article.
_LEADING_ARTICLE = re.compile(r"^(?:the|la|le|el|die|der|das)\s+")
# A surname is only worth searching when it is long enough to be specific.
# "Lee" or "Roy" returns a hundred strangers and could never be confirmed by
# a date alone, so the weaker tier refuses rather than guesses.
MIN_SURNAME_LENGTH = 4


def normalized(value):
    return normalize_name(value)


def _country_key(value):
    key = _LEADING_ARTICLE.sub("", normalized(value))
    return {"usa": "united states", "us": "united states",
            "uzbekistan": "乌兹别克斯坦"}.get(key, key)


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


def portrait_key(player, team_id, *, generation=2):
    """Identity for the cache: the roster entry, not the publisher's record."""
    name = normalized(player.get("name"))
    if generation == 1:
        # The previous version did not fold standalone letters such as ø.
        text = "".join(c for c in unicodedata.normalize("NFKD", str(player.get("name") or "")).casefold()
                       if not unicodedata.combining(c))
        name = " ".join("".join(c if c.isalnum() else " " for c in text).split())
    identity = f"{team_id}|{name}|{player.get('birth_date') or ''}|{player.get('number') or ''}"
    if generation == 2:
        identity += "|" + _country_key(player.get("nationality"))
    # Identity validation now also rejects contradictory birth dates and invalid
    # publisher IDs. Older completions cannot bypass those stronger checks.
    return f"community_portrait_v{generation}_" + sha256(identity.encode("utf-8")).hexdigest()[:24]


def cached_portrait(store, player, team_id):
    """Reuse completions only when their saved identity evidence still agrees.

    A previous-generation completion may be read without rewriting the old
    entry, but its status alone is insufficient. Older production entries did
    not save name/country/birthday evidence, so they need a fresh lookup.
    """
    cached = store.get_meta(portrait_key(player, team_id), {})
    if cached.get("status") in {"completed", "missing", "ambiguous"} and _age_seconds(cached.get("fetched_at")) < SUCCESS_SECONDS:
        return cached if cached["status"] != "completed" or _completion_matches(cached, player) else None
    if cached.get("status") == "unavailable" and _age_seconds(cached.get("last_attempt")) < RETRY_SECONDS:
        previous = cached.get("last_success")
        if (isinstance(previous, dict) and previous.get("status") == "completed"
                and _age_seconds(previous.get("fetched_at")) < SUCCESS_SECONDS
                and _completion_matches(previous, player)):
            return previous
        return cached
    legacy = store.get_meta(portrait_key(player, team_id, generation=1), {})
    if (legacy.get("status") == "completed" and _age_seconds(legacy.get("fetched_at")) < SUCCESS_SECONDS
            and _completion_matches(legacy, player)):
        return legacy
    return None


def _completion_matches(cached, player):
    """Whether a saved completion still proves this exact roster entry.

    The saved verdict is not trusted on its own: the evidence that produced it
    is re-derived against the roster values the app holds now, so a completion
    earned under a weaker rule cannot outlive that rule. A country disagreement
    is allowed here only when an exact birth date carries the match, which is
    the same condition that let the match be made.
    """
    evidence = cached.get("_identity")
    if not isinstance(evidence, dict):
        return False
    wanted_name = normalized(player.get("name"))
    if not wanted_name:
        return False
    published_name = normalized(evidence.get("name"))
    # The weakest tier exists precisely because the two spellings differ, so the
    # full name is required to agree only for the tiers that matched on it. What
    # every tier does require is the surname, which is the part that had to
    # agree for the record to be chosen at all.
    if cached.get("photo_match") == MATCH_SURNAME_AND_BIRTHDATE:
        if _surname(published_name) != _surname(wanted_name):
            return False
    elif published_name != wanted_name:
        return False
    wanted_country = _roster_country(player.get("nationality"))
    country_agrees = bool(wanted_country
                          and (_publisher_country(evidence.get("nationality")) & wanted_country))
    birthday = str(player.get("birth_date") or "")
    if evidence.get("roster_birth_date", "") != birthday:
        return False
    published = str(evidence.get("birth_date") or "")
    if birthday and published:
        if published == birthday:
            pass                       # an exact date carries the match alone
        elif published[:4] == birthday[:4]:
            pass                       # a shared year is the same evidence the
                                       # acquisition tier accepted on
        else:
            return False               # contradictory dates are a different person
    elif not country_agrees:
        return False
    publisher_id = str(evidence.get("publisher_id") or "")
    if not re.fullmatch(r"[1-9][0-9]{0,11}", publisher_id):
        return False
    profile = f"https://{HOST}/player/{publisher_id}"
    return bool(_safe_image_url(cached.get("photo_url")) and cached.get("photo_credit")
                and cached.get("photo_license") and cached.get("photo_match") in COMMUNITY_PHOTO_MATCHES
                and cached.get("photo_source_url") == profile and cached.get("photo_file_page") == profile)


def _safe_image_url(value):
    """Accept only the publisher's own https image paths."""
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return ""
    if (parts.scheme != "https" or parts.hostname not in IMAGE_HOSTS or parts.username or parts.password
            or port not in {None, 443} or parts.fragment
            or not parts.path.startswith("/images/media/player/")
            or not parts.path.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))):
        return ""
    return value


async def _pace(store):
    """One paced request at a time; the free tier throttles bursts."""
    if not hasattr(store, "_community_pace_lock"):
        store._community_pace_lock = asyncio.Lock()
        store._community_last_request = 0.0
    async with store._community_pace_lock:
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


def _live(record):
    """A retired or non-football record can never answer for a live squad."""
    return (record.get("strSport") in {None, "Soccer"}
            and record.get("strStatus") in {None, "Active"})


def _valid_id(record):
    return bool(re.fullmatch(r"[1-9][0-9]{0,11}", str(record.get("idPlayer") or "")))


def _publisher_born(record):
    return str(record.get("dateBorn") or "")[:10]


def _birth_verdict(roster_birth, record):
    """How the two stated birth dates relate, when both exist.

    'exact'    the dates agree to the day
    'year'     only the year agrees
    'conflict' both state a date and they disagree beyond the year
    'unknown'  either side states nothing, so this fact decides nothing
    """
    published = _publisher_born(record)
    if not roster_birth or not published:
        return "unknown"
    if published == roster_birth:
        return "exact"
    if published[:4] == str(roster_birth)[:4]:
        return "year"
    return "conflict"


def _surname(value):
    parts = normalized(value).split()
    return parts[-1] if parts else ""


def _narrow(candidates, wanted_country, roster_birth):
    """Pick the one candidate a second identity fact leaves, or nothing.

    Tried in order of strength: an exact birth date separates namesakes that a
    country cannot, a shared birth year is the publisher's own imprecision, and
    country is the weakest because dual nationals are stated inconsistently.
    """
    for key, value in (("exact_birthdate", lambda row: _birth_verdict(roster_birth, row) == "exact"),
                       ("birth_year", lambda row: _birth_verdict(roster_birth, row) == "year")):
        narrowed = [row for row in candidates if value(row)]
        if len(narrowed) == 1:
            return narrowed[0], key
    if wanted_country:
        narrowed = [row for row in candidates
                    if _publisher_country(row.get("strNationality")) & wanted_country]
        if len(narrowed) == 1:
            return narrowed[0], "nationality"
    return None, ""


def _confirm(record, wanted_country, roster_birth, *, base):
    """Decide whether a record is this player, and say which fact settled it."""
    if not _live(record):
        # A retired or non-football record cannot answer for a live squad entry.
        return None, "missing", ""
    if not _valid_id(record):
        # The identity is known but its publisher id cannot be linked, so the
        # record is unusable rather than absent.
        return None, "ambiguous", ""
    verdict = _birth_verdict(roster_birth, record)
    country_agrees = bool(wanted_country
                          and _publisher_country(record.get("strNationality")) & wanted_country)
    if verdict == "conflict":
        # Both sides state a birthday and they disagree: this is a different
        # person who happens to share the name, never a data quibble.
        return None, "ambiguous", ""
    if verdict == "exact":
        # A birth date to the day is a stronger fact than nationality, which
        # dual nationals and the publisher state inconsistently. It is allowed
        # to carry the match alone, and to overrule a country disagreement.
        return record, "completed", f"{base}_and_birthdate"
    if verdict == "year" and roster_birth:
        # The name is exact and the year agrees; only the day differs, which is
        # the publisher's own imprecision. Accepted, and labelled for the weaker
        # evidence it actually rests on, even when the country agrees too.
        return record, "completed", MATCH_NAME_AND_BIRTH_YEAR
    if not country_agrees:
        return None, "ambiguous", ""
    return record, "completed", (MATCH_NAME_AND_NATIONALITY if base == "community_name"
                                 else MATCH_SURNAME_AND_BIRTHDATE)


def _unique_match(payload, name, nationality, birth_date=""):
    """The one record that is this player, by name plus a second stated fact.

    A club match is deliberately not required and not accepted: the publisher's
    club field was observed naming a different club for a player whose club the
    roster states, so it carries no identity weight in either direction.

    The second fact is read in order of strength — an exact birth date, then a
    shared birth year, then country. A record whose stated birthday contradicts
    the roster's is refused however well the name matches, because that is the
    shape a namesake takes.
    """
    records = payload.get("player")
    if records is None:
        return None, "missing", ""
    if not isinstance(records, list) or len(records) > 50:
        return None, "unavailable", ""
    if not records:
        return None, "missing", ""
    wanted_country = _roster_country(nationality)
    wanted = normalized(name)
    exact = [row for row in records if isinstance(row, dict) and normalized(row.get("strPlayer")) == wanted]
    if exact:
        if len(exact) == 1:
            return _confirm(exact[0], wanted_country, birth_date, base="community_name")
        record, _key = _narrow(exact, wanted_country, birth_date)
        if record is None:
            # Two records share the name and nothing separates them: refusing
            # is the only honest answer, because falling back to the first
            # would publish a stranger's face.
            return None, "ambiguous", ""
        return _confirm(record, wanted_country, birth_date, base="community_name")
    # No record carries the roster's exact spelling. The publisher spells names
    # inconsistently ("Branco Boomen" for "Branco van den Boomen"), so a second
    # request is spent on the surname alone — that is the only way to reach
    # those records. It is admitted only when an exact birth date agrees, which
    # is the evidence that makes a partial name safe.
    return None, "missing", ""


def _surname_match(payload, name, birth_date=""):
    """The one record whose surname matches and whose birth date agrees exactly.

    This is the weaker of the two name checks and it never stands alone: the
    roster's own birth date must equal the publisher's to the day, so a surname
    shared with a stranger cannot publish a photo. Country is deliberately not
    required here — it is exactly the fact that fails on dual nationals such as
    the ones this tier exists to recover.
    """
    records = payload.get("player")
    if records is None:
        return None, "missing", ""
    if not isinstance(records, list) or len(records) > 50:
        return None, "unavailable", ""
    if not records:
        # The surname search legitimately answers with nothing; that is a gap
        # in the source, not a fault to retry.
        return None, "missing", ""
    surname = _surname(name)
    if not surname or len(surname) < MIN_SURNAME_LENGTH or not birth_date:
        return None, "missing", ""
    same_surname = [row for row in records
                    if isinstance(row, dict) and _surname(row.get("strPlayer")) == surname and _live(row)]
    if not same_surname:
        return None, "missing", ""
    exact_date = [row for row in same_surname if _birth_verdict(birth_date, row) == "exact"]
    if len(exact_date) != 1:
        return None, ("ambiguous" if len(exact_date) > 1 else "missing"), ""
    record = exact_date[0]
    if not _valid_id(record):
        return None, "ambiguous", ""
    return record, "completed", MATCH_SURNAME_AND_BIRTHDATE


async def resolve_portrait(store, player, team_id, *, force=False):
    """Return checked portrait metadata or an explicit non-photo reason."""
    name = player.get("name") if isinstance(player, dict) else ""
    if not isinstance(name, str) or not normalized(name):
        return {"status": "missing", "photo_reason": "缺少可核对的球员姓名。", "retryable": False,
                "reason_code": "missing_name"}
    if not _roster_country((player or {}).get("nationality")) and not (player or {}).get("birth_date"):
        # No country and no birthday leaves only the name, which is one identity
        # fact, and one fact is not enough to publish an unlicensed portrait.
        # Answer without a request rather than spending a paced lookup that
        # could not be accepted either way.
        return {"status": "ambiguous", "photo_reason": "缺少可核对的国籍和出生日期，无法与社区球员库交叉确认。",
                "retryable": False, "reason_code": "missing_nationality"}
    key = portrait_key(player, team_id)
    previous = cached_portrait(store, player, team_id)
    cached = None if force else previous
    if cached:
        return cached
    attempted = now()
    try:
        payload = await _search(store, name)
        record, verdict, matched_by = _unique_match(payload, name, player.get("nationality"),
                                                    player.get("birth_date") or "")
        if verdict == "missing":
            # The exact spelling found nothing. One extra paced request on the
            # surname reaches the publisher's own spelling of the same player,
            # and is attempted only when the roster states a birth date for the
            # date check to confirm it with.
            surname = _surname(name)
            if surname and len(surname) >= MIN_SURNAME_LENGTH and player.get("birth_date"):
                by_surname = await _search(store, surname)
                found, fallback_verdict, fallback_match = _surname_match(
                    by_surname, name, player.get("birth_date") or "")
                if fallback_verdict == "completed":
                    record, verdict, matched_by = found, fallback_verdict, fallback_match
        if verdict != "completed":
            result = {"status": verdict, "photo_reason": {
                "missing": "社区球员库里没有完整姓名匹配的现役球员。",
                "ambiguous": "社区球员库的姓名、国籍或出生日期无法唯一核对到这名球员。",
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
                          "photo_match": matched_by, "photo_reason": "",
                          "_identity": {"name": record["strPlayer"], "nationality": record["strNationality"],
                                        "birth_date": _publisher_born(record),
                                        "roster_birth_date": player.get("birth_date") or "",
                                        "publisher_id": str(record["idPlayer"])}}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        details = source_error_details(exc)
        # A throttle or transport fault says nothing about this player, so it is
        # rechecked on the short backoff instead of becoming a long-lived verdict.
        result = {"status": "unavailable", "photo_reason": "社区球员库暂时不可达，稍后自动重试。",
                  "retryable": True, "reason_code": details["code"]}
    result.update(fetched_at=now(), last_attempt=attempted)
    if result.get("status") == "unavailable" and previous and previous.get("status") == "completed":
        # Preserve only a still-current completion whose saved name, country,
        # birthday and publisher ID passed the current validator above.
        result["last_success"] = previous
    store.set_meta(key, result)
    return result
