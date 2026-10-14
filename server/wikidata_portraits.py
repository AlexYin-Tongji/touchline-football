"""Portrait lookup anchored on the roster's ESPN id, not on a name search.

The free community search returns one unsorted hit. This path does not use it.
Wikidata's P3681 (ESPN FC player ID) is the same numbering space as the roster's
``provider_id``. A single ``haswbstatement`` hit is only a pointer: the entity's
own P3681 claim has to repeat that id, and a day-precision birth date that
disagrees with the roster is a different person. The picture is the entity's
P18 file, and only after the existing Commons licence check.

A miss is cached and is not written onto the roster row. Another source may
still answer. A birth-date conflict is also not a photo, and it is not a
community namesake block — that flag stays reserved for the truncated name
search.
"""
from __future__ import annotations

import re
from hashlib import sha256

from . import wikimedia_portraits
from .ingestion import now, source_error_details
from .wikimedia_portraits import (
    COMPLETED_SECONDS, RETRY_SECONDS, SUCCESS_SECONDS, WIKIDATA, _age_seconds,
)

MATCH_ESPN_PROVIDER_ID = "wikidata_espn_provider_id"
_QID = re.compile(r"Q[1-9]\d{0,11}")
_ESPN_ID = re.compile(r"[0-9]{1,10}")


def portrait_key(player):
    provider_id = str((player or {}).get("provider_id") or "")
    birth = str((player or {}).get("birth_date") or "")
    material = f"{provider_id}|{birth}"
    return "wikidata_espn_portrait_v1_" + sha256(material.encode("utf-8")).hexdigest()[:24]


def _proof(cached, player):
    identity = cached.get("_identity") if isinstance(cached, dict) else None
    if not isinstance(identity, dict):
        return False
    provider_id = str((player or {}).get("provider_id") or "")
    if not _ESPN_ID.fullmatch(provider_id) or identity.get("provider_id") != provider_id:
        return False
    if identity.get("roster_birth_date", "") != str((player or {}).get("birth_date") or ""):
        return False
    if not _QID.fullmatch(str(identity.get("qid") or "")):
        return False
    return bool(cached.get("photo_url") and cached.get("photo_credit") and cached.get("photo_license")
                and cached.get("photo_file_page") and cached.get("photo_match") == MATCH_ESPN_PROVIDER_ID
                and cached.get("photo_source_url") == "https://www.wikidata.org/wiki/" + identity["qid"])


def cached_portrait(store, player):
    """Reuse a stored answer only inside its own window."""
    cached = store.get_meta(portrait_key(player), {})
    status = cached.get("status")
    if status == "completed" and _age_seconds(cached.get("fetched_at")) < COMPLETED_SECONDS and _proof(cached, player):
        return cached
    if status in {"missing", "rejected"} and _age_seconds(cached.get("fetched_at")) < SUCCESS_SECONDS:
        return cached
    if status == "unavailable" and _age_seconds(cached.get("last_attempt")) < RETRY_SECONDS:
        return cached
    return None


def current_proof(store, player):
    """Whether the row's Wikidata photo still belongs to this roster id.

    The reuse window does not decide this. An expired cache is not evidence
    that the identity was withdrawn.
    """
    cached = store.get_meta(portrait_key(player), {})
    return cached.get("status") == "completed" and _proof(cached, player)


def _claim_values(rows):
    values = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get("rank") == "deprecated":
            continue
        value = ((row.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        values.append((row.get("rank") or "normal", value))
    return values


def _espn_ids(rows):
    return [value for _rank, value in _claim_values(rows)
            if isinstance(value, str) and _ESPN_ID.fullmatch(value)]


def _portrait_file(rows):
    named = [(rank, value) for rank, value in _claim_values(rows)
             if isinstance(value, str) and value and not value.startswith(("http://", "https://")) and len(value) <= 240]
    preferred = [value for rank, value in named if rank == "preferred"]
    if len(preferred) == 1:
        return preferred[0]
    if len(named) == 1:
        return named[0][1]
    return ""


def _births_agree(roster_birth, rows):
    """False only when both sides state a date and they disagree.

    A missing date on either side does not contradict the id. Day precision
    must match the day; coarser precision is compared only at that grain.
    """
    roster = str(roster_birth or "")[:10]
    stated = []
    for _rank, value in _claim_values(rows):
        if not isinstance(value, dict):
            continue
        stamp = str(value.get("time") or "")
        if len(stamp) < 11 or stamp[0] not in "+-":
            continue
        stated.append((stamp[1:11], value.get("precision")))
    if not roster or not stated:
        return True
    for text, precision in stated:
        if precision == 11 and text != roster:
            return False
        if precision == 10 and text[:7] != roster[:7]:
            return False
        if precision == 9 and text[:4] != roster[:4]:
            return False
        if precision not in {9, 10, 11} and text != roster:
            return False
    return True


def _gap(status, reason_code, photo_reason):
    return {"status": status, "photo_reason": photo_reason, "retryable": False, "reason_code": reason_code}


async def _claims(store, qid, prop):
    payload = await wikimedia_portraits.request_json(store, WIKIDATA, {
        "action": "wbgetclaims", "format": "json", "entity": qid, "property": prop})
    claims = payload.get("claims") if isinstance(payload, dict) else None
    rows = claims.get(prop) if isinstance(claims, dict) else None
    return rows if isinstance(rows, list) else []


async def _resolve(store, player, provider_id):
    found = await wikimedia_portraits.request_json(store, WIKIDATA, {
        "action": "query", "format": "json", "list": "search", "srlimit": "2",
        "srsearch": f"haswbstatement:P3681={provider_id}"})
    hits = ((found.get("query") or {}).get("search") or []) if isinstance(found, dict) else []
    total = ((found.get("query") or {}).get("searchinfo") or {}).get("totalhits") if isinstance(found, dict) else None
    titles = [hit.get("title") for hit in hits if isinstance(hit, dict) and _QID.fullmatch(str(hit.get("title") or ""))]
    if total not in (None, 1) or len(titles) != 1:
        return _gap("missing", "wikidata_no_espn_id", "维基数据没有与该球员编号唯一对应的条目。")
    qid = titles[0]
    if _espn_ids(await _claims(store, qid, "P3681")) != [provider_id]:
        return _gap("rejected", "rejected", "维基数据条目没有复述这个球员编号，未采用照片。")
    if not _births_agree(player.get("birth_date"), await _claims(store, qid, "P569")):
        return _gap("rejected", "wikidata_birth_conflict", "维基数据上该编号对应的出生日期与阵容不一致，未采用照片。")
    file_name = _portrait_file(await _claims(store, qid, "P18"))
    if not file_name:
        return _gap("missing", "no_free_image", "维基数据没有一张可唯一核对的肖像文件。")
    licensed = await wikimedia_portraits._commons_license(store, file_name)
    if not licensed:
        return _gap("rejected", "license_not_free", "维基共享资源未证实这张肖像具有自由许可。")
    return {**licensed, "status": "completed", "photo_match": MATCH_ESPN_PROVIDER_ID,
            "photo_source_url": f"https://www.wikidata.org/wiki/{qid}", "photo_reason": "",
            "_identity": {"provider_id": provider_id, "qid": qid,
                          "roster_birth_date": str(player.get("birth_date") or ""),
                          "file": file_name}}


async def resolve_portrait(store, player, *, force=False):
    """Checked Commons portrait for this roster id, or an explicit non-photo."""
    provider_id = str((player or {}).get("provider_id") or "") if isinstance(player, dict) else ""
    if not _ESPN_ID.fullmatch(provider_id):
        return _gap("missing", "missing_provider_id", "缺少可核对的球员编号，无法按编号检索维基数据照片。")
    key = portrait_key(player)
    cached = None if force else cached_portrait(store, player)
    if cached:
        return cached
    attempted = now()
    try:
        result = await _resolve(store, player, provider_id)
    except Exception as exc:
        details = source_error_details(exc)
        result = {"status": "unavailable", "photo_reason": "维基数据暂时不可达，稍后自动重试。",
                  "retryable": True, "reason_code": details.get("code") or "source_unavailable"}
    result.update(fetched_at=now(), last_attempt=attempted)
    store.set_meta(key, result)
    return result
