"""Conservative Wikimedia portrait metadata resolver for checked squad members."""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from datetime import date, datetime, timezone
from hashlib import sha256
from urllib.parse import quote, urlencode, urlsplit

from bs4 import BeautifulSoup

from .club_library import TEAM_ALIASES
from .ingestion import IngestionError, fetch_document, now, published_time
from .news_sources import AMBIGUOUS_GLOBAL_ALIASES, OTHER_CLUB_IDENTITIES, global_feed_mentions_team
from .research_transport import blocking_request, shared_request

WIKIPEDIA = "en.wikipedia.org"
WIKIDATA = "www.wikidata.org"
COMMONS = "commons.wikimedia.org"
HOSTS = frozenset({WIKIPEDIA, WIKIDATA, COMMONS})
SUCCESS_SECONDS = 86400
RETRY_SECONDS = 3600
MAX_SEARCH_RESULTS = 4
MAX_ARTICLES = 2
MAX_JSON_BYTES = 512 * 1024
WIKIMEDIA_MIN_INTERVAL_SECONDS = 2.2
PHOTO_FIELDS = ("photo_url", "photo_credit", "photo_source_url", "photo_file_page",
                "photo_license", "photo_restrictions", "photo_match", "photo_reason")


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


async def request_json(store, host, params):
    """One pinned, coordinated API read; no unreviewed URL or image bytes."""
    if host not in HOSTS or not isinstance(params, dict):
        raise IngestionError("球员照片来源地址未获允许。")
    if not hasattr(store, "_wikimedia_pace_locks"):
        store._wikimedia_pace_locks = {}
        store._wikimedia_last_request = {}
    async with store._wikimedia_pace_locks.setdefault(host, asyncio.Lock()):
        gap = WIKIMEDIA_MIN_INTERVAL_SECONDS - (time.monotonic() - store._wikimedia_last_request.get(host, 0))
        if gap > 0:
            await asyncio.sleep(gap)
        store._wikimedia_last_request[host] = time.monotonic()
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
    return await shared_request(store, "portrait:" + url, host, factory)


def _page(payload):
    pages = payload.get("query", {}).get("pages", {}) if isinstance(payload, dict) else {}
    return next(iter(pages.values()), {}) if isinstance(pages, dict) else {}


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


def _active_team_ids(claims):
    active = set()
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
        if starts is None or ends is None or any(start > today for start in starts) or any(end < today for end in ends):
            continue
        active.add(qid)
    return active


async def _wikidata_club(store, qid, team_id, birth_date):
    if not re.fullmatch(r"Q[1-9]\d{0,11}", str(qid)):
        return False
    response = await request_json(store, WIKIDATA, {"action": "wbgetentities", "format": "json",
                                                 "ids": qid, "props": "claims"})
    claims = response.get("entities", {}).get(qid, {}).get("claims", {})
    occupations = [row.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id")
                   for row in claims.get("P106", [])]
    if occupations and "Q937857" not in occupations:
        return False
    if birth_date:
        known = [str(row.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("time", ""))[1:11]
                 for row in claims.get("P569", [])]
        if known and birth_date not in known:
            return False
    ids = _active_team_ids(claims)
    if not ids or len(ids) > 5:
        return False
    labels = await request_json(store, WIKIDATA, {"action": "wbgetentities", "format": "json",
                                                 "ids": "|".join(sorted(ids)), "props": "labels", "languages": "en"})
    aliases = {_name(alias) for alias in TEAM_ALIASES.get(team_id, ())
               if alias not in AMBIGUOUS_GLOBAL_ALIASES.get(team_id, ())}
    matched = [candidate for candidate in ids
               if _name(labels.get("entities", {}).get(candidate, {}).get("labels", {}).get("en", {}).get("value")) in aliases]
    return len(matched) == 1 and len(ids) == 1


def _license_metadata(payload, file_name):
    record = _page(payload)
    if _file_title(record.get("title")) != _file_title("File:" + file_name):
        return None
    image = next(iter(record.get("imageinfo") or []), {})
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
    key = "commons_license_v1_" + sha256(file_name.encode("utf-8")).hexdigest()[:24]
    cached = store.get_meta(key, {})
    if not force and cached.get("status") == "completed" and _age_seconds(cached.get("fetched_at")) < SUCCESS_SECONDS:
        return cached.get("photo")
    if not force and cached.get("status") == "rejected" and _age_seconds(cached.get("fetched_at")) < SUCCESS_SECONDS:
        return None
    payload = await request_json(store, COMMONS, {"action": "query", "format": "json", "titles": "File:" + file_name,
                                                 "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": 300})
    photo = _license_metadata(payload, file_name)
    store.set_meta(key, {"status": "completed" if photo else "rejected", "fetched_at": now(), "photo": photo})
    return photo


def _portrait_key(player, team_id):
    identity = f"{team_id}|{_name(player.get('name'))}|{player.get('birth_date') or ''}"
    return "wikimedia_portrait_v1_" + sha256(identity.encode("utf-8")).hexdigest()[:24]


def cached_portrait(store, player, team_id):
    cached = store.get_meta(_portrait_key(player, team_id), {})
    if (cached.get("status") in {"completed", "rejected", "missing"}
            and _age_seconds(cached.get("fetched_at")) < SUCCESS_SECONDS
            or cached.get("status") == "unavailable" and _age_seconds(cached.get("last_attempt")) < RETRY_SECONDS):
        return cached
    return None


async def resolve_portrait(store, player, team_id, *, force=False):
    """Return checked image metadata or an explicit non-image reason."""
    name = player.get("name") if isinstance(player, dict) else ""
    birth_date = player.get("birth_date") if isinstance(player, dict) else ""
    if not isinstance(name, str) or not _name(name) or team_id not in TEAM_ALIASES:
        return {"status": "rejected", "photo_reason": "缺少可核对的球员或俱乐部身份。"}
    key = _portrait_key(player, team_id)
    cached = None if force else cached_portrait(store, player, team_id)
    if cached:
        return cached
    attempted = now()
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
                if club != "confirmed" and not await _wikidata_club(store, props.get("wikibase_item"), team_id, birth_date):
                    reasons.append("无法独立核对当前俱乐部身份。")
                    continue
                matched.append(article)
            if len(matched) != 1:
                result = {"status": "rejected", "photo_reason": "存在多个同名现役球员，身份无法消歧。" if matched else
                          next(iter(reasons), "无法独立核对球员身份。")}
            else:
                article = matched[0]
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
    except Exception:
        result = {"status": "unavailable", "photo_reason": "维基媒体照片来源暂不可用，未采用未经核验图片。"}
    result.update(fetched_at=now(), last_attempt=attempted)
    store.set_meta(key, result)
    return result
