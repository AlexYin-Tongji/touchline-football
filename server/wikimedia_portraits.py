"""Conservative Wikimedia portrait metadata resolver for checked squad members."""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from datetime import date, datetime, timezone
from hashlib import sha256
from urllib.parse import quote, unquote, urlencode, urlsplit

from bs4 import BeautifulSoup

from .club_library import TEAM_ALIASES
from .ingestion import IngestionError, now, published_time, source_error_details
from .news_sources import AMBIGUOUS_GLOBAL_ALIASES, OTHER_CLUB_IDENTITIES, global_feed_mentions_team
from .research_transport import blocking_request, shared_request, fetch_reviewed_portrait_document as fetch_document
from .player_names import checked_wikidata_reading, cjk_label

WIKIPEDIA = "en.wikipedia.org"
WIKIDATA = "www.wikidata.org"
COMMONS = "commons.wikimedia.org"
HOSTS = frozenset({WIKIPEDIA, WIKIDATA, COMMONS})
SUCCESS_SECONDS = 86400
# A completed result is a licensed photo of a real person, not a perishable
# reading. While it was held to the same 24h as a verdict, every roster rebuild
# that landed more than a day after the fetch threw the photo away — and because
# the community fallback only sees rows Wikimedia never filled, those players
# got no second chance. Verdicts keep the short window; completions get a long one.
COMPLETED_SECONDS = 30 * 86400
# A verified identity, a missing page and a licence refusal are durable answers.
# A throttle or transport fault is not, so it expires quickly and is rechecked.
RETRY_SECONDS = 300
MAX_SEARCH_RESULTS = 4
MAX_ARTICLES = 2
MAX_JSON_BYTES = 512 * 1024
WIKIMEDIA_MIN_INTERVAL_SECONDS = 2.2
# Each entry is the wait before the next attempt; the last attempt waits 0.
# Long enough to clear the coordinator's 30s failure cooldown.
WIKIMEDIA_RETRY_BACKOFF_SECONDS = (5.0, 32.0, 0.0)
PHOTO_FIELDS = ("photo_url", "photo_credit", "photo_source_url", "photo_file_page",
                "photo_license", "photo_restrictions", "photo_match", "photo_reason",
                "retryable", "reason_code")


def _name(value):
    text = "".join(c for c in unicodedata.normalize("NFKD", str(value or "")).casefold()
                   if not unicodedata.combining(c))
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


def _candidate_name(title, birth_date):
    if not isinstance(title, str) or len(title) > 180:
        return ""
    match = re.fullmatch(r"\s*(.*?)\s*(?:\(([^()]*)\))?\s*", title)
    if not match:
        return ""
    qualifier = match[2]
    if qualifier:
        details = re.fullmatch(r"footballer(?:,\s*born\s+(\d{4}))?", qualifier, re.I)
        if not details or details[1] and birth_date and details[1] != birth_date[:4]:
            return ""
    return _name(match[1])


def _file_title(value):
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFC", value).replace("_", " ").split()).casefold()


def _age_seconds(value):
    parsed = published_time(value)
    return (datetime.now(timezone.utc) - datetime.fromisoformat(parsed)).total_seconds() if parsed else float("inf")


def _safe_url(value, host, path_prefix):
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        parts = urlsplit(value)
        if (parts.scheme == "https" and parts.hostname == host and parts.port in {None, 443}
                and not parts.username and not parts.password and not parts.fragment
                and parts.path.startswith(path_prefix)):
            return value
    except ValueError:
        pass
    return ""


async def _pace(store):
    """One shared interval across every Wikimedia host.

    Pacing was per-host, so Wikipedia, Wikidata and Commons each ran at the
    full rate and the combined traffic still tripped HTTP 429. A single shared
    clock holds the whole Wikimedia budget to one request per interval.
    """
    if not hasattr(store, "_wikimedia_pace_lock"):
        store._wikimedia_pace_lock = asyncio.Lock()
        store._wikimedia_last_request = 0.0
    async with store._wikimedia_pace_lock:
        gap = WIKIMEDIA_MIN_INTERVAL_SECONDS - (time.monotonic() - store._wikimedia_last_request)
        if gap > 0:
            await asyncio.sleep(gap)
        # Stamped after the wait, so a queued caller cannot reuse the slot the
        # previous request already consumed.
        store._wikimedia_last_request = time.monotonic()


async def request_json(store, host, params):
    """One pinned, coordinated API read; no unreviewed URL or image bytes."""
    if host not in HOSTS or not isinstance(params, dict):
        raise IngestionError("球员照片来源地址未获允许。")
    url = f"https://{host}/w/api.php?{urlencode(params)}"

    async def factory():
        body, final_url, content_type = await blocking_request(fetch_document, url, timeout=22,
            allowed_hosts={host}, max_body=MAX_JSON_BYTES,
            content_types=("application/json", "text/json", "text/plain"))
        if (not _safe_url(final_url, host, "/w/api.php") or urlsplit(final_url).path != "/w/api.php"
                or "json" not in content_type or len(body) > MAX_JSON_BYTES):
            raise IngestionError("维基媒体接口响应不完整，未采用照片。")
        try:
            result = json.loads(body)
        except (UnicodeError, ValueError, RecursionError):
            raise IngestionError("维基媒体接口资料无法读取，未采用照片。") from None
        if not isinstance(result, dict) or result.get("error"):
            raise IngestionError("维基媒体接口拒绝了照片查询。")
        return result

    # A throttle is transient by definition, so back off and retry instead of
    # surfacing 429 as "this player has no photo". The backoff must outlast the
    # coordinator's own failure cooldown (research_transport.FAILURE_COOLDOWN_
    # SECONDS), otherwise the retry is answered from that cached error without
    # touching the network and every attempt is wasted.
    last = None
    for attempt, wait in enumerate(WIKIMEDIA_RETRY_BACKOFF_SECONDS):
        await _pace(store)
        try:
            return await shared_request(store, "portrait:" + url, host, factory, force=attempt > 0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last = exc
            if source_error_details(exc)["code"] != "http_rate_limited":
                raise
            if wait:
                await asyncio.sleep(wait)
    raise last


def _page(payload):
    pages = payload.get("query", {}).get("pages", {}) if isinstance(payload, dict) else {}
    return next(iter(pages.values()), {}) if isinstance(pages, dict) else {}


def _source_withdrawn(record):
    """Explicit privacy/deletion evidence overrides preserved image metadata."""
    return isinstance(record, dict) and (
        any(record.get(flag) is True for flag in ("private", "deleted", "restricted"))
        or any(flag in record for flag in
               ("missing", "invalid", "suppressed", "filehidden", "texthidden", "contenthidden")))


def _birth_date(text):
    match = re.search(r"\bborn\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})\b", text, re.I)
    if not match:
        return ""
    try:
        return datetime.strptime(match[1], "%d %B %Y").date().isoformat()
    except ValueError:
        return ""


def _current_club(extract, team_id):
    lead = str(extract or "").split("\n", 1)[0]
    sentence = re.split(r"(?<=[.!?])\s+", lead, maxsplit=1)[0]
    sentence = re.split(r"\b(?:formerly|previously|before joining|having played)\b", sentence, maxsplit=1, flags=re.I)[0]
    play = re.search(r"\b(?:plays|currently plays|currently playing)\b", sentence, re.I)
    if play:
        current = re.split(r"\b(?:on loan from|loaned from|after leaving|after moving from|after transferring from|having left|following (?:a |his |her |their )?departure from)\b|[,;]", sentence[play.start():],
                           maxsplit=1, flags=re.I)[0]
        for other in OTHER_CLUB_IDENTITIES.get(team_id, ()):
            current = re.sub(re.escape(other), " ", current, flags=re.I)
        if global_feed_mentions_team(team_id, {"title": current}):
            return "confirmed"
        # An explicit present-tense relationship to another club is contrary
        # evidence; historical mentions later in the article cannot rescue it.
        if re.search(r"\b(?:for|at)\b", current, re.I):
            return "other_club"
    return "unknown"


def _team_claim_ids(claims):
    """Split P54 memberships into currently-active and undated candidates.

    Wikidata career entries frequently carry no P580/P582 qualifiers at all --
    David Raya has five such claims and no dated ones. Treating a missing date
    as "not this club" silently discarded the whole career and rejected real
    players, so an undated claim is an unproven candidate instead. Only a claim
    that provably ended before today, or starts in the future, is excluded.
    """
    active, undated = set(), set()
    today = date.today()
    def qualifier_dates(claim, property_id):
        rows = claim.get("qualifiers", {}).get(property_id, [])
        dates = []
        for row in rows:
            value = row.get("datavalue", {}).get("value", {})
            stamp = value.get("time", "") if isinstance(value, dict) else ""
            try:
                dates.append(date.fromisoformat(stamp[1:11]))
            except (TypeError, ValueError):
                return None
        return dates
    for claim in claims.get("P54", []):
        if not isinstance(claim, dict) or claim.get("rank") == "deprecated":
            continue
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value", {})
        qid = value.get("id") if isinstance(value, dict) else None
        if not isinstance(qid, str) or not re.fullmatch(r"Q[1-9]\d{0,11}", qid):
            continue
        starts, ends = qualifier_dates(claim, "P580"), qualifier_dates(claim, "P582")
        if any(end < today for end in (ends or [])) or any(start > today for start in (starts or [])):
            continue
        (active if starts or ends else undated).add(qid)
    return active, undated


async def _club_qid_matches(store, ids, team_id):
    """True when this club label matches exactly one candidate and nothing else.

    Wikidata labels the club "Arsenal F.C.", which normalises to "arsenal f c"
    and never equals the stored alias "arsenal fc" -- the old equality test
    therefore rejected every real player. Punctuation is dropped before
    comparing so label and alias reduce to the same form, and the club id must
    still be the only candidate that matches. Non-Latin aliases are skipped:
    the Wikidata call below only reads English labels, and a Chinese alias
    would otherwise reduce to an empty string and match an empty label.
    """
    aliases = {_name(alias).replace(" ", "") for alias in TEAM_ALIASES.get(team_id, ())
               if alias not in AMBIGUOUS_GLOBAL_ALIASES.get(team_id, ())}
    aliases.discard("")
    if not aliases or not ids:
        return False
    labels = await request_json(store, WIKIDATA, {"action": "wbgetentities", "format": "json",
                                                 "ids": "|".join(sorted(ids)), "props": "labels", "languages": "en"})
    entities = labels.get("entities", {})
    matched = set()
    for candidate in ids:
        label = entities.get(candidate, {}).get("labels", {}).get("en", {}).get("value")
        if _name(label).replace(" ", "") in aliases:
            matched.add(candidate)
    return len(matched) == 1


async def _wikidata_club(store, qid, team_id, birth_date, *, identity=None):
    if not re.fullmatch(r"Q[1-9]\d{0,11}", str(qid)):
        return False
    response = await request_json(store, WIKIDATA, {"action": "wbgetentities", "format": "json",
                                                 "ids": qid, "props": "claims|labels", "languages": "zh-hans|zh"})
    entity = response.get("entities", {}).get(qid, {})
    if (not isinstance(entity, dict) or entity.get("id", qid) != qid
            or any(flag in entity for flag in ("missing", "invalid", "suppressed"))
            or any(entity.get(flag) is True for flag in ("private", "deleted", "restricted"))):
        return False
    claims = entity.get("claims", {})
    occupations = [row.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
                   for row in claims.get("P106", [])]
    if occupations and "Q937857" not in occupations:
        return False
    if birth_date:
        known = [str(row.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("time", ""))[1:11]
                 for row in claims.get("P569", [])]
        if known and birth_date not in known:
            return False
    active, undated = _team_claim_ids(claims)
    if len(active) > 5:
        return False
    # A dated current membership is the strongest evidence, but a career with
    # only undated claims still proves the club -- as long as exactly one of
    # those candidates is this club and no other dated team contradicts it.
    matched = await _club_qid_matches(store, active, team_id)
    if not matched and not active and undated:
        matched = await _club_qid_matches(store, undated, team_id)
    if matched and identity is not None:
        labels = entity.get("labels") if isinstance(entity.get("labels"), dict) else {}
        for language in ("zh-hans", "zh"):
            label = labels.get(language, {})
            if isinstance(label, dict) and label.get("language") == language and cjk_label(label.get("value")):
                identity.update(qid=qid, label=label["value"], language=language,
                                club_qids=sorted(active or undated))
                break
    return matched


def _license_metadata(payload, file_name):
    record = _page(payload)
    if (not isinstance(record, dict) or _source_withdrawn(record)
            or _file_title(record.get("title")) != _file_title("File:" + file_name)):
        return None
    image = next(iter(record.get("imageinfo") or []), {})
    if not isinstance(image, dict) or _source_withdrawn(image):
        return None
    metadata = image.get("extmetadata") or {}
    def plain(key):
        value = metadata.get(key, {}).get("value", "")
        return BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)[:250]
    short, code = plain("LicenseShortName"), plain("License").lower()
    free_short = bool(re.fullmatch(r"CC (?:BY(?:-SA)?|0)(?: \d(?:\.\d)?)?|Public domain", short, re.I))
    free_code = bool(re.fullmatch(r"cc-(?:by(?:-sa)?|zero|0)(?:-\d(?:\.\d)?)?|pd-[a-z0-9-]+", code))
    if not (free_short and free_code):
        return None
    artist = plain("Artist")
    photo_url = _safe_url(image.get("thumburl"), "thumb.wikimedia.org", "/wikipedia/commons/thumb/")
    if not photo_url:
        photo_url = _safe_url(image.get("thumburl"), "upload.wikimedia.org", "/wikipedia/commons/thumb/")
    page_url = _safe_url(image.get("descriptionurl"), COMMONS, "/wiki/File:")
    if not artist or not photo_url or not page_url:
        return None
    return {"photo_url": photo_url, "photo_credit": artist, "photo_file_page": page_url,
            "photo_license": short, "photo_restrictions": plain("Restrictions")}


async def _commons_license(store, file_name, *, force=False):
    key = _commons_key(file_name)
    cached = store.get_meta(key, {})
    if (not force and cached.get("status") == "completed" and not _source_withdrawn(cached)
            and not _source_withdrawn(cached.get("photo"))
            and _age_seconds(cached.get("fetched_at")) < SUCCESS_SECONDS):
        return cached.get("photo")
    if not force and cached.get("status") == "rejected" and _age_seconds(cached.get("fetched_at")) < SUCCESS_SECONDS:
        return None
    payload = await request_json(store, COMMONS, {"action": "query", "format": "json", "titles": "File:" + file_name,
                                                 "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": 300})
    photo = _license_metadata(payload, file_name)
    store.set_meta(key, {"status": "completed" if photo else "rejected", "fetched_at": now(), "photo": photo})
    return photo


def _commons_key(file_name):
    return "commons_license_v1_" + sha256(file_name.encode("utf-8")).hexdigest()[:24]


def _commons_denial(store, photo):
    """A newer file verdict also governs portraits checked before that verdict."""
    page = _safe_url(photo.get("photo_file_page"), COMMONS, "/wiki/File:")
    if not page:
        return None
    file_name = unquote(urlsplit(page).path[len("/wiki/File:"):])
    # Commons links use underscores; pageimage normally supplies spaces.
    names = {file_name, file_name.replace("_", " ")}
    records = [store.get_meta(_commons_key(name), {}) for name in names]
    current = min((record for record in records if record),
                  key=lambda record: _age_seconds(record.get("fetched_at")), default={})
    if (current.get("status") in {"rejected", "missing"} or _source_withdrawn(current)
            or _source_withdrawn(current.get("photo"))):
        return current
    return None


def _rejected_photo(record, reason):
    return {"status": "rejected", "photo_reason": reason, "retryable": False,
            "reason_code": "source_withdrawn", "fetched_at": record.get("fetched_at")}


def _completed_portrait(store, cached):
    return (isinstance(cached, dict) and not _source_withdrawn(cached)
            and cached.get("status") == "completed" and _fresh_enough(cached)
            and cached.get("photo_match") == "wikimedia_verified_club"
            and all(cached.get(field) for field in
                    ("photo_url", "photo_credit", "photo_source_url", "photo_file_page", "photo_license"))
            and not _commons_denial(store, cached))


def _portrait_key(player, team_id, generation=2):
    identity = f"{team_id}|{_name(player.get('name'))}|{player.get('birth_date') or ''}"
    return f"wikimedia_portrait_v{generation}_" + sha256(identity.encode("utf-8")).hexdigest()[:24]


def cached_portrait(store, player, team_id):
    """Read the current cache, then the previous generation.

    ``_portrait_key`` changed from v1 to v2 when the old logic stored a throttle
    as a one-hour "no photo" verdict. Bumping the namespace fixed that, but it
    also orphaned every entry the earlier generation had already verified — a
    completed, licence-checked photo became permanently unreadable because the
    key that addressed it was no longer computed. Read the previous generation
    as a fallback so a version bump can no longer delete a real result.
    """
    cached = store.get_meta(_portrait_key(player, team_id), {})
    if not cached:
        cached = store.get_meta(_portrait_key(player, team_id, generation=1), {})
    if _source_withdrawn(cached):
        verdict = _rejected_photo(cached, "维基媒体照片来源当前不可公开核对。")
        return verdict if _fresh_enough(verdict) else None
    if cached.get("status") == "completed":
        if _completed_portrait(store, cached):
            return cached
        denial = _commons_denial(store, cached)
        verdict = _rejected_photo(denial, "Commons 当前记录已拒绝这张图片，等待重新核验。") if denial else None
        return verdict if verdict and _fresh_enough(verdict) else None
    previous = cached.get("last_success")
    if cached.get("status") == "unavailable" and _completed_portrait(store, previous):
        return previous
    if _fresh_enough(cached):
        return cached
    return None


def cached_reading(store, player, team_id):
    """Only an explicitly verified name proof in the current cache is eligible.

    Photo completion, a previous cache generation or a last-success photo is
    insufficient to revive a Chinese name after an unknown or removed source.
    """
    cached = store.get_meta(_portrait_key(player, team_id), {})
    if _source_withdrawn(cached) or cached.get("name_status") != "verified":
        return None
    return checked_wikidata_reading(cached, player, team_id=team_id)


def _fresh_enough(cached):
    """Whether a stored verdict still answers this question today.

    A verified free-licence portrait is a stable fact about a player, and the
    community source already trusts its own completion for a week. Holding the
    stronger, licence-checked source to a shorter life than the weaker one only
    threw away good photos. Verdicts stay durable; faults still expire fast.
    """
    status = cached.get("status")
    if status == "completed":
        return _age_seconds(cached.get("fetched_at")) < COMPLETED_SECONDS
    if status in {"rejected", "missing"}:
        return _age_seconds(cached.get("fetched_at")) < SUCCESS_SECONDS
    if status == "unavailable":
        return _age_seconds(cached.get("last_attempt")) < RETRY_SECONDS
    return False


RETRYABLE_REASON = "维基媒体查询过于频繁或暂时不可达，稍后自动重试。"

# Only these answer the question "does this player have an acceptable photo?".
# Everything else is a failure to find out, and must be rechecked rather than
# frozen as a permanent verdict about the player.
_IDENTITY_VERDICT_CODES = frozenset({
    "ambiguous_identity", "missing_article", "no_free_image", "license_not_free",
})


def _failure_kind(exc):
    """Classify any exception into a verdict or a retryable failure.

    This used to fall through to ``rejected`` for anything outside a short
    allowlist, so an unclassified transport or parse fault became a permanent
    "this player has no photo" answer. Failures now stay retryable unless the
    cause is positively a verdict about this player.
    """
    details = source_error_details(exc)
    code = str(details.get("code") or "source_unavailable")
    if code in _IDENTITY_VERDICT_CODES:
        return False, code
    return True, code


def _retryable(exc):
    """A throttle or transport fault is not a verdict about this player.

    The cache used to store every exception as "unavailable" for a full hour,
    so one HTTP 429 froze twenty Arsenal players as unphotographable. Those are
    retried on the short backoff; only a real identity/licence rejection is held
    for the long term.
    """
    return _failure_kind(exc)[0]


async def resolve_portrait(store, player, team_id, *, force=False):
    """Return checked image metadata or an explicit non-image reason."""
    name = player.get("name") if isinstance(player, dict) else ""
    birth_date = player.get("birth_date") if isinstance(player, dict) else ""
    if not isinstance(name, str) or not _name(name) or team_id not in TEAM_ALIASES:
        return {"status": "rejected", "photo_reason": "缺少可核对的球员或俱乐部身份。"}
    key = _portrait_key(player, team_id)
    previous = cached_portrait(store, player, team_id)
    cached = None if force else previous
    if cached:
        return cached
    attempted = now()
    name_reading = {}
    try:
        search = await request_json(store, WIKIPEDIA, {"action": "query", "format": "json", "list": "search",
                                                      "srsearch": f'"{name}" footballer', "srlimit": MAX_SEARCH_RESULTS})
        titles = [hit.get("title", "") for hit in search.get("query", {}).get("search", [])]
        exact = list(dict.fromkeys(title for title in titles
            if _candidate_name(title, birth_date) == _name(name)))
        exact.sort(key=lambda title: ("(" in title, title))
        if not exact:
            result = {"status": "missing", "photo_reason": "维基百科未找到完整姓名匹配的球员页面。"}
        elif len(exact) > MAX_ARTICLES:
            result = {"status": "rejected", "photo_reason": "存在多个同名球员，身份无法消歧。"}
        else:
            matched = []
            reasons = []
            for title in exact:
                article = _page(await request_json(store, WIKIPEDIA, {"action": "query", "format": "json",
                    "titles": title, "prop": "pageprops|pageimages|extracts", "piprop": "thumbnail|name",
                    "pithumbsize": 300, "exintro": 1, "explaintext": 1}))
                if _source_withdrawn(article):
                    reasons.append("百科页面当前不可公开核对。")
                    continue
                if _candidate_name(article.get("title"), birth_date) != _name(name):
                    reasons.append("百科页面身份与球员完整姓名不一致。")
                    continue
                props = article.get("pageprops") or {}
                desc = str(props.get("wikibase-shortdesc") or "")
                extract = str(article.get("extract") or "")
                if ("disambiguation" in props or not re.search(r"\bfootballer\b|\bfootball player\b", desc, re.I)
                        or not re.search(r"\bfootballer\b|\bfootball player\b", extract[:500], re.I)):
                    reasons.append("页面未证实主体为足球运动员。")
                    continue
                known_birth = _birth_date(extract[:300])
                if birth_date and known_birth and birth_date != known_birth:
                    reasons.append("出生日期与已核对阵容不一致。")
                    continue
                club = _current_club(extract, team_id)
                if club == "other_club":
                    reasons.append("页面记载的当前俱乐部与阵容不一致。")
                    continue
                identity = {}
                if club != "confirmed" and not await _wikidata_club(store, props.get("wikibase_item"), team_id, birth_date, identity=identity):
                    reasons.append("无法独立核对当前俱乐部身份。")
                    continue
                matched.append((article, identity))
            if len(matched) != 1:
                result = {"status": "rejected", "photo_reason": "存在多个同名现役球员，身份无法消歧。" if matched else
                          next(iter(reasons), "无法独立核对球员身份。")}
            else:
                article, identity = matched[0]
                if identity:
                    name_reading = {"name_status": "verified", "name_zh": identity["label"],
                        "name_zh_source": "Wikidata 中文标签", "name_zh_source_url": "https://www.wikidata.org/wiki/" + identity["qid"],
                        "name_zh_checked_at": now(), "name_zh_identity": {
                            **identity, "method": "wikidata_existing_identity_check", "name": name,
                            "team_id": team_id, "roster_birth_date": birth_date or "",
                            "provider_id": str(player.get("provider_id") or ""),
                            "article_title": article["title"],
                            "article_url": "https://en.wikipedia.org/wiki/" + quote(article["title"].replace(" ", "_")),
                            "unique_article": True, "identity_checked": True, "public": True}}
                file_name = article.get("pageimage")
                if not isinstance(file_name, str) or not file_name or not article.get("thumbnail", {}).get("source"):
                    result = {"status": "missing", "photo_reason": "球员页面没有可核对的自由图片。"}
                else:
                    license_data = await _commons_license(store, file_name, force=force)
                    if not license_data:
                        result = {"status": "rejected", "photo_reason": "Commons 未证实图片具有自由许可与署名。"}
                    else:
                        article_url = "https://en.wikipedia.org/wiki/" + quote(article["title"].replace(" ", "_"))
                        result = {**license_data, "status": "completed", "photo_source_url": article_url,
                                  "photo_match": "wikimedia_verified_club", "photo_reason": ""}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Keep the real class of failure in the record instead of collapsing
        # every cause to one sentence. An unclassified fault is a failure to
        # find out, not a verdict about the player, so it is recorded with its
        # real reason code and rechecked on the short backoff.
        retryable, code = _failure_kind(exc)
        if retryable:
            result = {"status": "unavailable", "photo_reason": RETRYABLE_REASON,
                      "retryable": True, "reason_code": code}
        else:
            result = {"status": "rejected", "photo_reason": "维基媒体资料无法核对，未采用未经核验图片。",
                      "retryable": False, "reason_code": code}
    result.update(name_status="unknown", fetched_at=now(), last_attempt=attempted)
    result.update(name_reading)
    if (result.get("status") == "unavailable" and previous and previous.get("status") == "completed"
            and previous.get("photo_match") == "wikimedia_verified_club"
            and all(previous.get(field) for field in
                    ("photo_url", "photo_credit", "photo_source_url", "photo_file_page", "photo_license"))):
        result["last_success"] = previous
    store.set_meta(key, result)
    return result
