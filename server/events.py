"""Conservative, source-grounded event grouping. This is not fact verification."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import urlsplit

# Aliases identify mentioned people; they do not assert current club membership.
ENTITY_ALIASES = {
    "saka": ("saka", "萨卡"), "rice": ("declan rice", "rice", "赖斯"),
    "odegaard": ("odegaard", "ødegaard", "厄德高"), "saliba": ("saliba", "萨利巴"),
    "gabriel": ("gabriel magalhaes", "gabriel magalhães", "加布里埃尔"),
    "jesus": ("gabriel jesus", "热苏斯"), "havertz": ("havertz", "哈弗茨"),
    "martinelli": ("martinelli", "马丁内利"), "timber": ("timber", "廷贝尔"),
    "calafiori": ("calafiori", "卡拉菲奥里"), "arteta": ("arteta", "阿尔特塔", "阿特塔"),
    "salah": ("salah", "萨拉赫"), "van_dijk": ("van dijk", "范戴克"),
    "mac_allister": ("mac allister", "麦卡利斯特"), "szoboszlai": ("szoboszlai", "索博斯洛伊"),
    "wirtz": ("wirtz", "维尔茨"), "isak": ("isak", "伊萨克"),
    "alisson": ("alisson", "阿利松"), "barcola": ("barcola", "巴尔科拉"),
    "pedri": ("pedri", "佩德里"), "gavi": ("gavi", "加维"),
    "yamal": ("yamal", "亚马尔"), "raphinha": ("raphinha", "拉菲尼亚"),
    "lewandowski": ("lewandowski", "莱万"), "flick": ("flick", "弗里克"),
    "lautaro": ("lautaro", "劳塔罗"), "thuram": ("thuram", "图拉姆"),
    "barella": ("barella", "巴雷拉"), "dimarco": ("dimarco", "迪马尔科"),
    "calhanoglu": ("calhanoglu", "çalhanoğlu", "恰尔汗奥卢"),
}


def normalized(value):
    return unicodedata.normalize("NFKC", value or "").casefold()


def contains(value, phrase):
    value, phrase = normalized(value), normalized(phrase)
    if re.fullmatch(r"[a-z0-9 -]+", phrase):
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", value))
    return phrase in value


def entities(value):
    return sorted(key for key, aliases in ENTITY_ALIASES.items() if any(contains(value, alias) for alias in aliases))


def suggest_stage(title, text):
    """Quote a narrowly matched claim; negated/speculative phrases never confirm."""
    subject = (title + "\n" + text[:8000]).strip()
    injury = bool(re.search(r"injur|fitness|rehabilitat|recover|back in training|return.{0,15}training|伤病|伤情|伤势|受伤|康复|复出|恢复合练|缺阵", subject, re.I))
    transfer = bool(re.search(r"transfer|sign(?:s|ed|ing)?\b|contract|loan|转会|签约|续约|租借", subject, re.I))
    kind = "injury" if injury else "transfer" if transfer else "team"
    rules = []
    if injury:
        rules = [
            (r"ruled out|will miss|unavailable for|确认缺阵|确定缺阵|无法出场", "来源称缺阵"),
            (r"back in (?:full )?training|return(?:ed|s)? to (?:full )?training|恢复(?:全部|完整)?合练|重返训练", "来源称恢复训练"),
            (r"in (?:the )?(?:matchday )?squad|named in.{0,20}squad|进入(?:比赛)?名单", "来源称进入名单"),
            (r"fitness (?:test|update)|doubt|assess|出战成疑|待评估", "出场情况待评估"),
        ]
    elif transfer:
        rules = [
            (r"sign(?:s|ed) (?:a |his |her )?(?:new |long.term )?contract|签署.{0,12}合同|完成签约|正式加盟", "来源称已签约"),
            (r"agreement (?:has been |was )?reached|达成协议", "来源称达成协议"),
            (r"talks|interest|linked|rumou?r|谈判|有意|传闻", "转会报道 · 待核实"),
        ]
    for pattern, label in rules:
        for match in re.finditer(pattern, subject, re.I):
            sentence_start = max(subject.rfind(mark, 0, match.start()) for mark in ("\n", ".", "。", "!", "?")) + 1
            prefix = subject[max(sentence_start, match.start() - 70):match.start()]
            if re.search(r"\b(?:not|no|never|unlikely|could|may|might|hope|hopes|expected)\b|尚未|并未|没有|不会|可能|预计|有望", prefix, re.I):
                continue
            excerpt = subject[max(sentence_start, match.start() - 60):match.end() + 110].split("\n")[0]
            return {"kind": kind, "label": label + " · 未独立核验", "excerpt": excerpt[:220], "method": "source_phrase", "verified": False}
    return {"kind": kind, "label": "伤情资料 · 待核实" if injury else "转会资料 · 待核实" if transfer else "来源记录 · 待核实", "excerpt": "", "method": "topic_only", "verified": False}


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _url_identity(value):
    """The address with tracking removed, the same way the report snapshot reads it.

    Grouping and snapshotting have to agree on what "the same article" means.
    When they do not, one layer collapses a duplicate and the other cannot see
    it, which is how one story ends up as two events and two cards.
    """
    if not value:
        return ""
    try:
        from .research import article_url
        return article_url(value)
    except (ValueError, TypeError, ImportError):
        return value.split("#")[0]


def _token_set(value):
    return set(re.findall(r"[a-z]{3,}|[" + "一-鿿" + r"]{2}", normalized(value)))


def find_related_event(store, team_id, title, text, url="", published_at=None):
    """Join the same article, or a second telling of the same story.

    Three tells, in order of strength:

    1. the same address, after tracking removal, is the same article;
    2. a shared named person *and* a matching headline is the same story, even
       when two publishers filed it under different editors or under a
       different topic. Requiring ``kind in {injury, transfer}``, as this used
       to, meant a press conference covered by four outlets and any ordinary
       team story could never be grouped at all;
    3. the same person, the same topic and a close headline within two days.

    What is still refused is the case the old rule existed to protect: two
    different pieces of news about the same player. "Saka signs" and "Saka
    injured" name the same person and must stay apart, which is why a shared
    name alone is never enough.
    """
    records = store.list("events", "live", team_id)
    evidence = {item["id"]: item for item in store.list("evidence", "live", team_id)}
    identity = _url_identity(url)
    for event in reversed(records):
        if identity and any(_url_identity(evidence.get(item, {}).get("url")) == identity
                            for item in event.get("evidence_ids", [])):
            return event
    kind = suggest_stage(title, text)["kind"]
    keys = entities(title)
    if not keys:
        return None
    current_time = _timestamp(published_at)
    left = _token_set(title)
    for event in reversed(records):
        if not set(event.get("entity_keys", entities(event.get("title", "")))) & set(keys):
            continue
        prior_items = [evidence[item] for item in event.get("evidence_ids", []) if item in evidence]
        for prior in reversed(prior_items):
            prior_time = _timestamp(prior.get("published_at"))
            if current_time and prior_time and abs((current_time - prior_time).total_seconds()) > 14 * 86400:
                continue
            similar = SequenceMatcher(None, normalized(title), normalized(prior.get("title", ""))).ratio()
            # Wording repeats when two outlets cover one event.
            if similar >= .82 or len(left & _token_set(prior.get("title", ""))) >= 3:
                return event
            # Same person, same topic, much closer date: a same-day report of
            # one press conference. Stricter on the wording so a season of
            # stories about one player does not collapse into one event.
            if (kind == event.get("kind") and similar >= .55 and current_time and prior_time
                    and abs((current_time - prior_time).total_seconds()) <= 2 * 86400):
                return event
    return None
    current_time = _timestamp(published_at)
    for event in reversed(records):
        if event.get("kind") != kind or event.get("entity_keys", entities(event.get("title", ""))) != keys:
            continue
        prior_items = [evidence[item] for item in event.get("evidence_ids", []) if item in evidence]
        for prior in reversed(prior_items):
            prior_time = _timestamp(prior.get("published_at"))
            if not current_time or not prior_time or abs((current_time - prior_time).total_seconds()) > 14 * 86400:
                continue
            # Different injuries/transfers can involve the same person. Require
            # recognizable common context beyond just their name and topic.
            left = set(re.findall(r"[a-z]{3,}|[\u4e00-\u9fff]{2}", normalized(title)))
            right = set(re.findall(r"[a-z]{3,}|[\u4e00-\u9fff]{2}", normalized(prior.get("title", ""))))
            common = left & right
            similar = SequenceMatcher(None, normalized(title), normalized(prior.get("title", ""))).ratio()
            if similar >= .82 or len(common) >= 3:
                return event
    return None


def record_update(event, evidence, *, explicit_stage=None, prior_event=None):
    """Preserve every distinct body and the originating source, including conflict."""
    suggestion = suggest_stage(evidence["title"], evidence["text"])
    suggestion["evidence_id"] = evidence["id"]
    event["entity_keys"] = entities(evidence["title"])
    if event.get("kind") == "team" and suggestion["kind"] != "team":
        event["kind"] = suggestion["kind"]
    source = evidence.get("source_name") or urlsplit(evidence.get("url", "")).hostname or "未知来源"
    event["source_names"] = list(dict.fromkeys([*event.get("source_names", []), source]))
    prior_suggestion = event.get("stage_suggestion")
    known_dates = [_timestamp(version.get("published_at")) for version in event.get("versions", [])]
    known_dates = [stamp for stamp in known_dates if stamp]
    incoming_date = _timestamp(evidence.get("published_at"))
    historical = bool(incoming_date and known_dates and incoming_date < max(known_dates))
    event["stage_suggestion"] = suggestion
    versions = event.setdefault("versions", [])
    if not any(version.get("fingerprint") == evidence.get("fingerprint") for version in versions):
        versions.append({"at": evidence["fetched_at"], "published_at": evidence.get("published_at"),
                         "evidence_id": evidence["id"], "fingerprint": evidence.get("fingerprint"),
                         "source_name": source, "title": evidence["title"], "stage_suggestion": suggestion})
    if explicit_stage:
        event["stage"] = explicit_stage
        event["stage_auto"] = False
    elif not event.get("stage") or event["stage"] == "来源记录 · 待核实" or event.get("stage_auto"):
        event["stage"] = suggestion["label"]
        event["stage_auto"] = True
    if prior_suggestion and prior_suggestion.get("label") != suggestion["label"]:
        event["change"] = "来源的阶段说法有变化；请按发布时间核对原文，不能据此视为已确认。"
    if historical and prior_event:
        for key in ("title", "summary", "stage", "stage_auto", "stage_suggestion", "entity_keys"):
            if key in prior_event:
                event[key] = prior_event[key]
        event["change"] = "补录一份较早发布的资料，保留在时间线中；未将旧说法替换为最新进展。"
    event["source_note"] = "来源数量仅用于追溯，不代表独立确认；转载与相同正文不会提升可信度。"
    return event
