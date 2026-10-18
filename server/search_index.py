"""A derived, persistent search index for local evidence.

The body of a collected article is deliberately not stored: ``Store._prepare``
drops it and keeps one hour of it in a process-local cache. That is the right
privacy boundary, but it used to mean ``search_evidence`` could only see titles
after a restart — every body term scored zero and every excerpt was empty.

This module keeps what a search needs and nothing a reader would recognise as
prose: token frequencies, character offsets, entity ids, categories and the
source tier. No sentence of any article is written to disk here. A hit is turned
back into text by reading the original source, exactly as before.

Scoring is BM25 over two fields (title and the article body) with a recency
half-life and a small source-tier lift. The previous implementation summed
boolean substring matches, so a long preview that happened to repeat the word
"比赛" scored as high as the article that actually named the player.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from datetime import datetime, timezone

from .events import ENTITY_ALIASES, contains, entities

# Two-letter English words carry little signal in football headlines and cost an
# index entry each; single-character CJK runs are kept because they are words.
STOP_LATIN = {"the", "and", "for", "with", "what", "how", "does", "about", "please", "latest", "news",
              "update", "updates", "team", "today", "recent", "follow", "track", "this", "has", "have",
              "any", "our", "its", "from", "continue", "check", "tell", "find", "me", "us", "to", "is",
              "are", "in", "on", "of", "at", "by", "as", "be", "it", "we", "he", "she", "they", "his",
              "her", "their", "not", "but", "or", "if", "will", "would", "can", "could", "said", "says",
              "one", "two", "three", "all", "some", "there", "here", "was", "were", "been", "being",
              "do", "did", "had", "when", "who", "whom", "why", "than", "then", "into", "out", "up",
              "more", "most", "also", "just", "now", "get", "got", "make", "makes", "made", "new"}
# Chinese function words. A one-character particle carries no topic and, as a
# bigram component, manufactures terms such as 的言 and 了什.
STOP_CJK_CHARS = set("的了着吗呢吧啊呀哦嗯是在我你他她它们这那和与及或就都也还又很更最不没")
# Two- and three-character questions words that survive bigram generation.
STOP_CJK = {"我们", "你们", "他们", "什么", "怎么", "怎么", "为什么", "哪些", "哪个", "如何", "目前",
            "现在", "今天", "最近", "最新", "近况", "消息", "情况", "一下", "看看", "有没", "没有",
            "可以", "能否", "是否", "就是", "还是", "以及", "关于", "这个", "那个", "球队", "比赛",
            "问题", "觉得", "认为", "知道", "想知", "请问", "帮我", "说说", "介绍", "解释", "分析",
            "怎样", "怎么样", "多少", "多久", "是否", "的话", "之后", "之前", "以后", "以前"}
GENERIC_TERMS = {"news", "latest", "update", "updates", "消息", "最新", "最近", "近况", "动态", "情况"}
# Headline furniture. These appear in the title of a large share of articles on
# one topic, so they are not evidence that an article is about the question.
# "latest"/"news" in particular identify a column, not a subject.
TITLE_NOISE = {"latest", "news", "update", "updates", "live", "report", "reports", "exclusive",
               "watch", "video", "opinion", "analysis", "preview", "recap", "podcast"}

# A topic's terms are not interchangeable. "results", "start" and "best" occur in
# a large share of sports writing; "ruled out", "sidelined" and "unbeaten" occur
# when the article is actually about that thing. Treating them alike let a
# human-interest piece headed "school record" win a question about league form.
TOPIC_BRIDGE = {
    "injury": (("伤病", "伤情", "受伤", "缺阵", "伤停", "伤势", "复出", "伤愈", "停赛"),
               ("injury", "injured", "fitness", "miss", "return", "recovery"),
               ("ruled out", "sidelined", "unavailable", "doubt", "out for", "suspension", "setback")),
    "lineup": (("首发", "阵容", "名单", "轮换", "排兵", "布阵"),
               ("squad", "selection", "rotation"),
               ("lineup", "line-up", "starting xi", "team news", "starting eleven")),
    "tactics": (("战术", "阵型", "打法", "体系", "逼抢", "压迫", "出球", "踢法"),
                ("tactics", "tactical", "system", "style"),
                ("formation", "pressing", "build-up", "high line", "in possession")),
    "transfer": (("转会", "签约", "续约", "租借", "买人", "卖人", "引援"),
                 ("transfer", "deal", "target", "window"),
                 ("signing", "contract", "loan", "bid", "medical", "agree", "agreement")),
    "manager": (("帅位", "主教练", "教练", "主帅", "下课", "执教", "发布会"),
                ("manager", "boss", "coach"),
                ("verdict", "future", "pressure", "sack", "sacked", "under fire", "backing", "press conference")),
    "form": (("开局", "战绩", "状态", "表现", "排名", "积分", "输赢", "胜负", "连败", "不败"),
             ("form", "results", "points", "standing", "start", "record", "best"),
             ("worst", "ever", "unbeaten", "winless", "streak", "history", "league start", "worst start")),
    "result": (("赛后", "复盘", "怎么输", "怎么赢", "比分", "战报"),
               ("report", "result", "defeat", "victory", "draw"),
               ("full-time", "reaction", "match report")),
    "atmosphere": (("更衣室", "氛围", "内讧", "矛盾", "风波", "不满", "球迷"),
                   ("fans", "supporters", "row", "fallout"),
                   ("dressing room", "atmosphere", "unrest", "mutiny")),
}


def topic_bridge(query):
    """The source-side terms for each topic the question raises, by strength.

    Returned as ``{topic: (weak_terms, strong_terms)}``. Weak terms are common
    words that raise a topic without establishing it; strong terms are the
    phrases a writer uses when the article is about that topic. The ranking
    caller decides the weights; this function only knows the vocabulary.
    """
    value = _normalized(query).lower()
    matches = {}
    for topic, (asked, weak, strong) in TOPIC_BRIDGE.items():
        if any(phrase.lower() in value for phrase in asked):
            matches[topic] = (tuple(term for phrase in weak for term in tokens(phrase)),
                              tuple(term for phrase in strong for term in tokens(phrase)))
    return matches

# A body is indexed up to this many characters. The cap keeps one article's
# record in the low tens of kilobytes and matches what a reader can paginate.
INDEX_TEXT_LIMIT = 20_000
# Distinct terms per field. Beyond this the tail is almost all one-offs.
MAX_TERMS = 2_500
PASSAGE_CHARS = 700
RECENCY_HALF_LIFE_DAYS = 7.0
K1, B = 1.2, 0.75
TITLE_WEIGHT = 2.5
TIER_LIFT = {"OFFICIAL": 1.15, "DATASET": 1.10, "PRESS": 1.0, "COMMUNITY": 0.97, "DERIVED": 0.95}


def _normalized(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(value or ""))).strip()


def tokens(text):
    """Words for scoring: latin words, numbers, CJK unigrams and bigrams.

    CJK has no spaces, and the previous two-character sliding window produced
    terms such as ``有什`` and ``的比`` by crossing word boundaries. Bigrams are
    still valuable (they are what makes ``萨卡`` distinct from ``萨克``), so they
    are kept — but only inside a run that is not itself a stop phrase, and the
    stop-phrase list removes the common ones that caused false positives.
    """
    value = _normalized(text).lower()
    result = []
    for word in re.findall(r"[a-z0-9][a-z0-9'-]*", value):
        if len(word) > 1 and word not in STOP_LATIN:
            result.append(word)
    for run in re.findall(r"[一-鿿]+", value):
        for index, char in enumerate(run):
            if char in STOP_CJK_CHARS:
                continue
            result.append(char)
            if index + 1 < len(run):
                pair = run[index:index + 2]
                if pair not in STOP_CJK and not (set(pair) & STOP_CJK_CHARS):
                    result.append(pair)
    return result


def frequencies(text):
    counts = {}
    for term in tokens(text):
        counts[term] = counts.get(term, 0) + 1
    if len(counts) > MAX_TERMS:
        keep = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:MAX_TERMS]
        counts = dict(keep)
    return counts


def title_key(title):
    """A same-story key: case, punctuation and spacing removed, nothing else."""
    value = _normalized(title).lower()
    value = re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20] if value else ""


def passage_offsets(text):
    """Character offsets of stable passages, without storing their text."""
    offsets, start, length = [], 0, len(text or "")
    for paragraph in re.split(r"\n+", text or ""):
        base = start
        remaining = paragraph
        while remaining:
            end = min(PASSAGE_CHARS, len(remaining))
            if end < len(remaining):
                boundaries = [match.end() for match in re.finditer(r"[.!?。！？](?:\s+|$)", remaining[:end])]
                if boundaries and boundaries[-1] >= 80:
                    end = boundaries[-1]
                else:
                    space = remaining.rfind(" ", 350, end)
                    if space > 0:
                        end = space
            offsets.append([base, base + end])
            base += end
            remaining = remaining[end:]
        start += len(paragraph) + 1
    return offsets[:400]


def entity_keys(text, store=None):
    """Entity ids mentioned in a title and the start of its body.

    Two sources: the static table (headline classification, unchanged) and, when
    a store is available, the roster readings, which are how a Chinese question
    and an English article name the same person.
    """
    value = _normalized(text)
    keys = {key for key, aliases in ENTITY_ALIASES.items()
            if any(contains(value, alias) for alias in aliases)}
    if store is not None:
        try:
            from .entity_names import named_entities
            keys |= named_entities(store, value)
        except Exception:
            pass
    return sorted(keys)


def source_tier(evidence):
    """The reviewed tier for a source, or PRESS when the host is unclassified."""
    try:
        from .news_source_tiers import by_host
        host = (evidence.get("url") or "").split("/")[2].lower() if "//" in (evidence.get("url") or "") else ""
        tier = by_host(host) if host else None
        if isinstance(tier, str) and tier.upper() in TIER_LIFT:
            return tier.upper()
    except Exception:
        pass
    return "PRESS"


def build_record(evidence, store=None):
    """The persisted index entry for one evidence row.

    Built from the body *before* ``Store._prepare`` removes it. When the body is
    unavailable (a fixture row, an imported note) the title is still indexed, so
    the record is never empty and never lies about having read text.
    """
    body = str(evidence.get("text") or "")[:INDEX_TEXT_LIMIT]
    title = str(evidence.get("title") or "")
    return {
        "id": evidence["id"],
        "team_id": evidence.get("team_id"),
        "mode": evidence.get("mode"),
        "body_hash": evidence.get("body_hash") or hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "title_tokens": frequencies(title),
        "body_tokens": frequencies(body),
        "body_length": max(1, len(tokens(body))),
        "title_length": max(1, len(tokens(title))),
        "passages": passage_offsets(body),
        "entities": entity_keys(title + "\n" + body[:4000], store),
        "clubs": clubs_mentioned(title + "\n" + body[:4000], store) if store is not None else [],
        "title_key": title_key(title),
        "source_tier": source_tier(evidence),
        "published_at": evidence.get("published_at"),
        "fetched_at": evidence.get("fetched_at"),
        "spoiler": bool(evidence.get("spoiler")),
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }


def _idf(total, frequency):
    # BM25's usual form, floored so a term present in most documents still
    # contributes a little instead of turning the score negative.
    return math.log(1 + (total - frequency + 0.5) / (frequency + 0.5))


def _recency(record, now):
    stamp = record.get("published_at") or record.get("fetched_at")
    if not stamp:
        return 1.0
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 1.0
    age_days = max(0.0, (now - parsed).total_seconds() / 86400)
    return 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)


def _term_score(query_terms, field, length, average, idf):
    matched, weight = 0.0, 0.0
    for term, term_weight in query_terms.items():
        frequency = field.get(term, 0)
        if not frequency:
            continue
        # A rebuilt record has no body on disk, so its body length is the 1 that
        # `build_record` floors it to. Length normalisation over that unit is
        # meaningless and used to lift every bodyless record to the same floor,
        # which is how an Arsenal transfer column outscored the fixture preview.
        if length > 1:
            saturation = frequency * (K1 + 1) / (frequency + K1 * (1 - B + B * length / average))
        else:
            saturation = frequency * (K1 + 1) / (frequency + K1)
        matched += term_weight * idf.get(term, 0.0) * saturation
        # Only terms that actually occur in this field share the denominator.
        # Dividing by every query term made an article that named the player
        # three times score 2.6 out of something — the alias spellings for the
        # same person were never going to appear, and they flattened the result.
        weight += term_weight
    return matched / weight if weight else 0.0


def rank(records, query_terms, *, limit=8, entities_required=None, clubs_required=None, team_id=None,
         strong_terms=(), idf=None, now=None):
    """Score and rank index records for one already-tokenised query.

    ``query_terms`` maps a term to its weight. Entity names arrive at a weight
    that dominates ordinary words; callers build that map so the weighting
    policy lives with the question, not in here.

    ``entities_required`` gates the result the way the rest of the product does:
    a question that names a person should not be answered from an article that
    never mentions them. Without the gate, Chinese filler bigrams (``的言``,
    ``论风``) matched unrelated Chinese pages well enough to outrank the article
    that actually quoted him. ``clubs_required`` is the same rule for the club
    named in the question; both gates step aside when the article corpus itself
    cannot satisfy them, because "we do not know this name" is not "this article
    is unrelated".

    ``strong_terms`` are the topic words that establish an article is about the
    question's subject rather than merely mentioning its words. An article
    carrying one is lifted; an article carrying none is not removed, because a
    topic word is a hint and the reader still decides.

    ``idf`` is corpus-wide document frequencies. Computing it from the ranked
    subset instead made rarity depend on which team was asked: ``马丁内斯`` was
    common inside one squad's material and rare across it.
    """
    now = now or datetime.now(timezone.utc)
    if not records or not query_terms:
        return []
    if entities_required:
        gated = [record for record in records
                 if entities_required & set(record.get("entities") or [])]
        if gated:
            records = gated
    if clubs_required:
        # A fixture question names both clubs; the article that answers it names
        # both too. An article about only the opponent is background, not the
        # answer, so the gate is the intersection of every club named. If the
        # corpus has no such article the gate falls back to the current club
        # (every row here), because an empty result would hide the material we
        # do have.
        gated = [record for record in records
                 if clubs_required <= set(record.get("clubs") or [])]
        if not gated and team_id:
            gated = [record for record in records
                     if team_id in set(record.get("clubs") or [])]
        if gated:
            records = gated
    total = len(records)
    if idf is None:
        frequency = {}
        for record in records:
            for term in query_terms:
                if term in record["title_tokens"] or term in record["body_tokens"]:
                    frequency[term] = frequency.get(term, 0) + 1
        idf = {term: _idf(total, count) for term, count in frequency.items()}
    average = sum(record["body_length"] for record in records) / total or 1.0
    strong = set(strong_terms)
    scored = []
    for record in records:
        title = _term_score(query_terms, record["title_tokens"], record["title_length"], average, idf)
        # A five-word headline ("Simi has a school record") matched one term and
        # scored as if it were about it, because the denominator is only that
        # term. Football headlines are normally eight to fifteen words; a
        # headline shorter than that is a caption, not a topic.
        brevity = min(1.0, record["title_length"] / 8)
        body = _term_score(query_terms, record["body_tokens"], record["body_length"], average, idf)
        score = (TITLE_WEIGHT * title * brevity + body) * _recency(record, now) * TIER_LIFT.get(record.get("source_tier"), 1.0)
        if strong and (strong & set(record.get("title_tokens") or {})):
            score *= 1.6
        if score > 0:
            scored.append((score, record))
    scored.sort(key=lambda row: row[0], reverse=True)
    return scored[:limit]


def group(rows):
    """Collapse the same story reported by several outlets.

    A headline repeated on three sites used to occupy three of the eight result
    slots. The group keeps the best-scoring member and lists the rest, so a
    second outlet is visible without crowding out a different story.
    """
    groups = {}
    order = []
    for score, record in rows:
        key = record.get("title_key") or record["id"]
        if key not in groups:
            groups[key] = {"score": score, "primary": record, "also": []}
            order.append(key)
        else:
            groups[key]["also"].append(record["id"])
            # A verified source outranks a reprint of the same headline even
            # when the reprint's title happens to tokenise better.
            if TIER_LIFT.get(record.get("source_tier"), 1.0) > TIER_LIFT.get(groups[key]["primary"].get("source_tier"), 1.0):
                groups[key]["also"].append(groups[key]["primary"]["id"])
                groups[key]["primary"] = record
    return [{"score": groups[key]["score"], "primary": groups[key]["primary"],
             "also_reported_by": groups[key]["also"]} for key in order]


def story_rows(records):
    """The records that are articles, not rendered data dumps.

    ``team_data_*`` is the structured snapshot, deliberately rendered as one long
    readable document so its fields can be quoted. Ranking it against articles
    made it win every question that mentioned the club's name: it is the longest
    text in the corpus and repeats that name throughout. It is still evidence,
    but it is returned as a separate section rather than competing for story
    slots.
    """
    return [record for record in records if not str(record.get("id", "")).startswith("team_data_")]


def search(records, query, team_id=None, store=None, *, limit=8):
    """Rank one question against one team's records, grouped by story.

    The single entry point the tools use, so weighting, gating, corpus IDF and
    duplicate collapsing cannot drift apart between callers.
    """
    if not records:
        return []
    weights = query_weights(query, team_id, store)
    gate = query_entities(query, store)
    # Only the club that is not the current one constrains the result: every
    # article in this slice is already about the current club.
    clubs = {club for club in query_clubs(query, store) if club != team_id}
    strong = {term for _weak, terms in topic_bridge(query).values() for term in terms}
    stories = story_rows(records)
    if not stories:
        return []
    # IDF is corpus-wide across every team, then ranking happens inside the
    # team's slice: a person's name should read as rare because it is rare in
    # the whole corpus, not because this squad has few articles about him.
    idf = corpus_idf(records, weights)
    ranked = rank(stories, weights, limit=max(12, limit * 3), entities_required=gate,
                  clubs_required=clubs, team_id=team_id, strong_terms=strong, idf=idf)
    return group(ranked)[:limit]


def rebuild(store, *, limit=None):
    """Index evidence rows that predate the index, or whose record is missing.

    A collected article's body is already gone from disk by the time this runs,
    so the rebuilt record carries the title only. That is deliberate: writing a
    record with an empty body under the article's real ``body_hash`` made the
    live update path skip it later — the hash matched, so a genuine read never
    replaced the hollow record. The record keeps ``body_hash=None`` so the next
    real read knows it still owes a body profile.
    """
    records = []
    existing = {row.get("id") for row in store.list("search_index")}
    for item in store.list("evidence"):
        if item.get("id") in existing:
            continue
        if limit is not None and len(records) >= limit:
            break
        body = str(item.get("text") or "")
        record = build_record({**item, "text": body}, store)
        if not body:
            # Title-level only. The hash is kept in a separate field so the
            # live path can tell "indexed, body still owed" apart from
            # "indexed at this exact body".
            record["body_hash"] = None
            record["body_owed"] = True
        store.put("search_index", record)
        records.append(record["id"])
    return {"indexed": len(records), "skipped_existing": len(existing)}


def query_weights(query, team_id=None, store=None):
    """Turn a question into weighted index terms.

    Reuses the alias tables the rest of the product already treats as truth:
    a person's name is one concept with many spellings, and naming them in a
    question should make their articles win, not merely tie. Entity tokens carry
    a weight high enough to dominate the filler bigrams every spoken Chinese
    question produces — including the English spellings a roster reading stands
    for, which is the only way a Chinese question reaches an English article.
    """
    from .agent_tools import CONCEPTS, search_terms

    value = _normalized(query)
    weights = {}
    for term in search_terms(value):
        weights[term] = max(weights.get(term, 0), 1.0)
    entity_aliases = {}
    for key in entities(value):
        entity_aliases[key] = ENTITY_ALIASES.get(key, ())
    if store is not None:
        try:
            from .entity_names import aliases_for
            entity_aliases.update(aliases_for(store, value))
        except Exception:
            pass
    for aliases in entity_aliases.values():
        for alias in aliases:
            for term in tokens(alias):
                weights[term] = 16.0
    # The club the user is following. A question rarely names it (they are
    # already looking at that team), and the alias the source publishes —
    # "Man Utd" in a headline — shares no characters with 曼联. It is weighted
    # at 1, not higher: every article in this team's slice is already about the
    # club, so a heavy weight on its name only lets the outlet that repeats the
    # name most often win. What is rare is the subject of the question.
    if store is not None and team_id:
        try:
            team = store.get("teams", team_id) or {}
            for alias in [team.get("english_name"), *(team.get("aliases") or [])]:
                for term in tokens(alias):
                    weights[term] = max(weights.get(term, 0), 1.0)
        except Exception:
            pass
    # Opponents are the other half of a match question. `search_terms` strips
    # club names on purpose (the current club is implied), which left
    # "诺丁汉森林对阵阿森纳" with no club term at all — the question then ranked
    # on the word 伤病 alone and surfaced any Arsenal injury story. The opponent
    # is applied as a *gate* in `search`, not as a heavy weight: weighting it
    # made every Arsenal column outrank the one article about this fixture.
    if store is not None:
        try:
            for opponent in store.list("teams"):
                if opponent.get("id") == team_id:
                    continue
                aliases = [opponent.get("name"), opponent.get("english_name"), *(opponent.get("aliases") or [])]
                if not any(contains(value, alias) for alias in aliases if alias):
                    continue
                for alias in aliases:
                    for term in tokens(alias):
                        weights[term] = max(weights.get(term, 0), 1.0)
        except Exception:
            pass
    for group in CONCEPTS:
        if any(contains(value, word) for word in group):
            for word in group:
                for term in tokens(word):
                    weights[term] = max(weights.get(term, 0), 1.5)
    # Chinese topic words do not appear in English articles. The bridge maps
    # them to the words the sources actually print: the generic ones at a low
    # weight, the ones that only appear when the article is about the topic at
    # a weight above an ordinary word and below a name.
    for weak, strong in topic_bridge(value).values():
        for term in weak:
            weights[term] = max(weights.get(term, 0), 0.7)
        for term in strong:
            weights[term] = max(weights.get(term, 0), 2.4)
    if not weights:
        # A generic question still deserves the newest material rather than an
        # empty list; the caller decides whether that is a useful answer.
        for term in tokens(value):
            weights[term] = 1.0
    for term in GENERIC_TERMS & set(weights):
        weights[term] *= 0.35
    return weights


def query_entities(query, store=None):
    """The person ids a question names, for the mandatory-mention gate."""
    value = _normalized(query)
    found = set(entities(value))
    if store is not None:
        try:
            from .entity_names import named_entities
            found |= named_entities(store, value)
        except Exception:
            pass
    return found


def corpus_idf(records, terms):
    """Document frequency over the whole indexed corpus, not one team's slice."""
    total = len(records) or 1
    frequency = {}
    for record in records:
        for term in terms:
            if term in record["title_tokens"] or term in record["body_tokens"]:
                frequency[term] = frequency.get(term, 0) + 1
    return {term: _idf(total, count) for term, count in frequency.items()}


def _club_keys(store):
    """Every club identity and the spellings sources use for it."""
    try:
        return [(team.get("id"), [team.get("name"), team.get("english_name"), *(team.get("aliases") or [])])
                for team in store.list("teams") if team.get("id")]
    except Exception:
        return []


def clubs_mentioned(text, store):
    """The club ids a piece of text names, by any spelling."""
    value = _normalized(text)
    return sorted({club for club, aliases in _club_keys(store)
                   if any(contains(value, alias) for alias in aliases if alias)})


def query_clubs(query, store):
    """Clubs named in the question, for the mandatory-mention gate.

    A match question names both sides. Within one club's workspace the current
    club is already implied, so only the *other* club named is a real filter —
    but it is a strong one: "阿森纳这场" should not return a Chelsea transfer
    story that happens to contain the word 伤病.
    """
    if store is None:
        return set()
    value = _normalized(query)
    return {club for club, aliases in _club_keys(store) if any(contains(value, alias) for alias in aliases if alias)}

    """The records that are articles, not rendered data dumps.

    ``team_data_*`` is the structured snapshot, deliberately rendered as one long
    readable document so its fields can be quoted. Ranking it against articles
    made it win every question that mentioned the club's name: it is the longest
    text in the corpus and repeats that name throughout. It is still evidence,
    but it is returned as a separate section rather than competing for story
    slots.
    """
    return [record for record in records if not str(record.get("id", "")).startswith("team_data_")]
