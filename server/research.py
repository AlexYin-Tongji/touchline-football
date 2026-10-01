"""Bounded first-party news discovery and original-page reading, without APIs."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import getproxies
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup

from . import ingestion
from .events import ENTITY_ALIASES, contains, entities, normalized
from .club_library import RESEARCH_CATALOG, TRUSTED_SOURCE_HOSTS, TEAM_ALIASES
from .news_sources import (BBC_HOSTS, FEED_HOSTS, additional_sources, feed_published_time,
                           global_feed_mentions_team, readable_publisher_entry, REGIONAL_PUBLISHER_HOSTS,
                           shared_host, TYC_HOSTS)
from .research_transport import manual_refresh_scope


CATALOG = RESEARCH_CATALOG
TRUSTED_HOSTS = TRUSTED_SOURCE_HOSTS | FEED_HOSTS | REGIONAL_PUBLISHER_HOSTS | {"www.bbc.co.uk", "www.bbc.com", "bbc.com", "bbc.co.uk", "www.espn.com", "espn.com", "www.skysports.com", "skysports.com", "www.reuters.com", "reuters.com", "www.bing.com", "bing.com", "site.api.espn.com"}
for _host in ("football.london", "espn.co.uk", "goal.com", "eurosport.com", "lequipe.fr", "kicker.de", "marca.com", "as.com", "sport.es", "gazzetta.it", "corrieredellosport.it"):
    TRUSTED_HOSTS.update({_host, "www." + _host})
TOPICS = [
    ("伤病", "伤情", "受伤", "伤势", "缺阵", "injury", "injured", "fitness", "unavailable", "ruled out", "lesión", "lesiones", "lesionado", "lesionados", "lesão", "lesões", "blessure", "blessé", "blessés"),
    ("复出", "恢复", "合练", "训练", "training", "recovery", "rehabilitation", "return", "entrenamiento", "entrenamientos", "recuperación", "treino", "treinos", "recuperação", "entraînement", "entrainement", "retour"),
    ("转会", "签约", "续约", "租借", "transfer", "signing", "contract", "loan", "signed", "signs", "fichaje", "fichajes", "renovación", "contrato", "traspaso", "cesión", "contratação", "transferência", "renovação", "transfert", "mercato", "prêt", "prolongation", "contrat"),
    ("首发", "阵容", "名单", "line-up", "lineup", "squad", "team news"),
    ("赛前", "比赛", "赛程", "备战", "欧冠", "对手", "preview", "fixture", "match", "kickoff",
     "champions league", "trip", "travel", "team news", "opponent", "opposition", "pre-match", "prematch", "ahead of"),
    ("赛后", "复盘", "战报", "review", "report", "reaction", "result"),
    ("战术", "阵型", "逼抢", "压迫", "出球", "轮换", "tactics", "tactical", "formation", "pressing", "build-up", "rotation"),
    ("发布会", "采访", "主帅", "教练", "press conference", "interview", "manager", "coach", "entraîneur", "entraineur", "conférence", "conference"),
    ("停赛", "纪律", "禁赛", "申诉", "suspension", "suspended", "disciplinary", "appeal", "ban"),
]
CATEGORIES = {"all": "全部", "injury": "伤病", "training": "复出", "transfer": "转会", "squad": "首发",
              "preview": "赛前", "review": "赛后", "tactics": "战术", "interview": "发布会", "discipline": "停赛"}
INDEX_CACHE_SECONDS = 10 * 60
ARTICLE_CACHE_SECONDS = 20 * 60
STALE_INDEX_SECONDS = 24 * 60 * 60
STOP_WORDS = {"the", "and", "for", "with", "what", "how", "does", "about", "please", "latest", "news", "update", "updates", "team", "today", "recent", "follow", "track", "this", "has", "have", "any", "our", "its", "from", "continue", "check", "tell", "find", "me", "us", "to", "is", "are", "in", "on"}
DISCOVERY_NOTICE = ("检索范围为球队官网、媒体订阅和公开新闻搜索，搜索结果仅接受已允许的足球新闻域名。候选只按索引、标题和摘要筛选，调用 read_article 后才能引用。"
                    "人名未命中不代表综合新闻正文没有该球员的信息；必要时可改用球队加赛事、对手或赛前主题查找官方综合发布会，再阅读核对。"
                    "不得把其他球员的消息当作目标球员的进展。")
ARTICLE_CONCURRENCY = 3
ACCESS_REVIEW_SECONDS = 7 * 24 * 60 * 60
SOURCE_BACKOFF_SECONDS = (60, 5 * 60, 30 * 60, 2 * 60 * 60, 12 * 60 * 60)
ESPN_FAILURE_CACHE_SECONDS = 10 * 60


def _check_team(store, team_id):
    if not store.get("teams", team_id):
        raise ingestion.IngestionError("球队不存在")


def source_catalog(store, team_id):
    _check_team(store, team_id)
    configured = store.list("sources", "live", team_id)
    disabled_urls = {ingestion.normalize_url(item["url"]) for item in configured if not item.get("enabled", True)}
    sources, urls = [], set()
    team_name = store.get("teams", team_id).get("name", "球队")
    for source in [*CATALOG.get(team_id, []), *additional_sources(team_id)]:
        url = ingestion.normalize_url(source["url"])
        if url in disabled_urls or url in urls:
            continue
        name = (team_name + "官网" if source["id"].startswith("official_") else
                source.get("name") or ("BBC 体育 · " + team_name if source["id"].startswith("bbc_") else urlsplit(url).hostname))
        review = source.get("access_review") or {}
        checked = _stamp(review.get("checked_at"))
        # A successful forced recheck supersedes an older static access note;
        # later live failures still enter the separate expiring health backoff.
        last_ok = _stamp(source_health(store, source).get("last_ok_at"))
        review_active = (review.get("status") in {"restricted", "access_limited", "timeout"} and checked is not None
                         and 0 <= (datetime.now(timezone.utc) - checked).total_seconds() < ACCESS_REVIEW_SECONDS
                         and not (last_ok and last_ok > checked))
        sources.append({**source, "name": name, "team_id": team_id, "builtin": True,
                        "access_review_active": review_active})
        urls.add(url)
    for source in configured:
        url = ingestion.normalize_url(source["url"])
        if source.get("enabled", True) and url not in urls:
            sources.append({**source, "description": "你配置的公开来源", "builtin": False})
            urls.add(url)
    # No publisher is a fallback for another. A source with a live failure or an
    # active access note is tried FIRST, not hidden: it is exactly the one whose
    # fresh result the reader has not seen. Health records still throttle the
    # request itself, so this changes order without widening network use.
    sources.sort(key=lambda source: source["id"])
    return sources


def _source_health_key(source):
    return "source_health_v1:source:" + source["id"]


def _host_health_key(source):
    """Failures at transport level belong to the host, not to one feed path."""
    return "source_health_v1:host:" + str(urlsplit(source["url"]).hostname)


def source_health(store, source):
    if not hasattr(store, "get_meta"):
        return {}
    local = store.get_meta(_source_health_key(source), {})
    if not shared_host(source.get("url")):
        return local
    host = store.get_meta(_host_health_key(source), {})
    current = datetime.now(timezone.utc)
    for record in (local, host):
        until = _stamp(record.get("backoff_until"))
        if until and current < until:
            return record
    return max((local, host), key=lambda record: _stamp(record.get("last_fail_at") or record.get("last_ok_at"))
               or datetime.min.replace(tzinfo=timezone.utc))


def _source_in_cooldown(store, source):
    until = _stamp(source_health(store, source).get("backoff_until"))
    return bool(until and datetime.now(timezone.utc) < until)


def _record_source_health(store, source, *, error=None, attempt_started=None):
    if error is not None and attempt_started is None:
        # The shared coordinator can replay a sibling's failure. That caller
        # did not make a new transport attempt and must not extend backoff.
        return source_health(store, source)
    details = ingestion.source_error_details(error) if error is not None else None
    shares_host = shared_host(source.get("url"))
    shared_failure = (shares_host and details is not None
                      and details["code"] in {"dns_failed", "tls_certificate", "tls_error", "timeout",
                                              "connection_reset", "connection_error"})
    keys = [_source_health_key(source)]
    if shares_host:
        if error is None:
            keys.append(_host_health_key(source))
        elif shared_failure:
            keys = [_host_health_key(source)]
    checked = ingestion.now()
    with store.atomic() if hasattr(store, "atomic") else nullcontext():
        for key in keys:
            current = store.get_meta(key, {})
            if error is None:
                current.update(consecutive_failures=0, backoff_until=None, last_ok_at=checked,
                               last_error_kind=None, last_error_message=None, last_http_status=None)
            else:
                last_failure = _stamp(current.get("last_fail_at"))
                # Two physical requests may already be in flight for one host.
                # A request begun before the first failure belongs to that
                # wave, even when every caller used force=True.
                if last_failure and attempt_started <= last_failure:
                    continue
                failures = min(int(current.get("consecutive_failures") or 0) + 1, len(SOURCE_BACKOFF_SECONDS))
                seconds = SOURCE_BACKOFF_SECONDS[failures - 1]
                current.update(consecutive_failures=failures, last_fail_at=checked,
                               last_error_kind=details["code"], last_error_message=details["message"],
                               last_http_status=details.get("http_status"),
                               backoff_until=(datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat())
            store.set_meta(key, current)
    return current


def _source_hosts(source):
    host = urlsplit(source["url"]).hostname
    hosts = {host}
    if source.get("builtin"):
        hosts.update(source.get("allowed_hosts") or [])
        if source["id"].startswith("bbc_"):
            hosts.update(BBC_HOSTS)
        elif host and host.startswith("www."):
            hosts.add(host[4:])
        if not hosts <= TRUSTED_HOSTS:
            raise ingestion.IngestionError("内置来源域名配置不正确")
    return hosts


def _checked_url(url, hosts):
    result = ingestion.normalize_url(url)
    if urlsplit(result).hostname not in hosts:
        raise ingestion.IngestionError("文章或重定向超出了该来源的已允许域名")
    return result


def article_url(url):
    """Discard only known tracking parameters; article-identifying queries stay."""
    parts = urlsplit(ingestion.normalize_url(url))
    tracking = {"fbclid", "gclid", "dclid", "mc_cid", "mc_eid", "cmp", "CMP"}
    pairs = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key not in tracking]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), ""))


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


def source_failure(source, error):
    """Actionable publisher status without transport URLs or response bodies."""
    return {"source_id": source.get("id"), "source_name": str(source.get("name") or "来源")[:160],
            "host": urlsplit(source.get("url") or "").hostname or "", **ingestion.source_error_details(error)}


def unique_source_failures(errors):
    result, seen = [], set()
    for error in errors:
        if not isinstance(error, dict):
            continue
        # Regenerate the safe message rather than trusting an upstream body.
        safe = {key: error.get(key) for key in ("source_id", "source_name", "host") if error.get(key)}
        safe.update(ingestion.source_error_details(ingestion.IngestionError(str(error.get("message") or ""))))
        key = (safe.get("source_id"), safe.get("source_name"), safe["code"], safe.get("http_status"))
        if key not in seen and len(result) < 30:
            result.append(safe)
            seen.add(key)
    return result


def within_days(entry, days, *, now=None):
    if days is None:
        return True
    published = _stamp(entry.get("published_at"))
    current = now or datetime.now(timezone.utc)
    # Discovery time and sitemap modification dates cannot establish freshness.
    return bool(published and current - timedelta(days=days) <= published <= current + timedelta(minutes=5))


def article_categories(entry):
    value = " ".join(str(entry.get(key) or "") for key in ("title", "description", "text"))
    return [key for key, label in CATEGORIES.items() if key != "all"
            and any(group[0] == label and _topic_matches(value, group) for group in TOPICS)]


def _first_team_entry(entry):
    # Club news landing pages also contain youth and women's fixtures. Do not
    # silently mix those squads into this men's first-team workspace.
    value = " ".join(str(entry.get(key) or "") for key in ("title", "description"))
    value += " " + unquote(urlsplit(entry.get("url", "")).path).replace("-", " ")
    return (readable_publisher_entry(entry) and not re.search(
        r"\b(?:women|womens|wsl|femeni|femení|femenin[oa]s?|feminin[oa]s?|féminin(?:e|es|s)?|sub[ -]?\d{1,2}|u[ -]?\d{1,2}|under[ -]?\d{1,2}|premier league 2|"
        r"youth[ -]+(?:teams?|squads?|league|sector|cup)(?![ -]+(?:graduates?|products?|alumni)\b))\b|女足|青年队|青年联赛",
        value, re.I))


async def _fetch(url, source):
    hosts = _source_hosts(source)
    current = _checked_url(url, hosts)
    # Arbitrary user sources retain the pinned-IP transport. Known public
    # publishers may use the user's OS proxy: necessary on networks whose local
    # DNS cannot reach those publishers. Every redirect remains allowlisted.
    if not source.get("builtin") or not getproxies() or source["id"] in {"official_arsenal", "official_liverpool", "official_inter"}:
        from .research_transport import blocking_request
        body, final_url, content_type = await blocking_request(ingestion.fetch_document, current, allowed_hosts=hosts, timeout=25)
        _checked_url(final_url, hosts)
        return body, final_url, content_type
    if not hosts <= TRUSTED_HOSTS:
        raise ingestion.IngestionError("内置来源域名配置不正确")
    try:
        async with asyncio.timeout(22):
            async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=8), follow_redirects=False, trust_env=True,
                                         headers={"User-Agent": "Touchline/2.0 (personal football reader)", "Accept": "text/html,application/rss+xml,application/atom+xml,application/xml,text/xml"}) as client:
                for redirect in range(5):
                    # The OS proxy resolves these fixed publisher names. Local
                    # DNS can be intercepted on this network; it is not used to
                    # resolve proxy destinations. Arbitrary source domains never
                    # reach this branch, even after a redirect.
                    async with client.stream("GET", current) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if redirect == 4 or not response.headers.get("location"):
                                raise ingestion.IngestionError("来源重定向次数过多或目标缺失")
                            current = _checked_url(urljoin(current, response.headers["location"]), hosts)
                            continue
                        if not 200 <= response.status_code < 300:
                            raise ingestion.IngestionError(f"来源返回 HTTP {response.status_code}")
                        if response.headers.get("cf-mitigated", "").lower() == "challenge":
                            raise ingestion.IngestionError("来源返回了访问验证页面，暂时无法读取正文")
                        content_type = response.headers.get("content-type", "").lower()
                        if content_type and not any(value in content_type for value in ("text/", "xml", "xhtml")):
                            raise ingestion.IngestionError("来源未返回网页或订阅源文本")
                        length = response.headers.get("content-length", "")
                        if length.isdigit() and int(length) > ingestion.MAX_BODY:
                            raise ingestion.IngestionError("来源内容超过 2 MB 上限")
                        chunks, total = [], 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > ingestion.MAX_BODY:
                                raise ingestion.IngestionError("来源内容超过 2 MB 上限")
                            chunks.append(chunk)
                        return b"".join(chunks), current, content_type
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise ingestion.SourceConnectionError("来源请求超时，请稍后重试") from None
    except httpx.HTTPError as exc:
        details = ingestion.source_error_details(exc)
        message = details["message"] if details["code"] != "source_unavailable" else "无法连接来源，请检查网络或系统代理。"
        raise ingestion.SourceConnectionError(message) from None
    raise ingestion.IngestionError("来源重定向次数过多")


async def _fetch_shared(store, url, source, *, force=False, on_start=None):
    from .research_transport import shared_request
    hosts = _source_hosts(source)
    checked = _checked_url(url, hosts)
    key = (article_url(checked), source["id"], bool(source.get("builtin")), tuple(sorted(hosts)))
    async def fetch_once():
        if on_start:
            on_start(datetime.now(timezone.utc))
        return await _fetch(checked, source)
    return await shared_request(store, key, urlsplit(checked).hostname, fetch_once, force=force)


def _xml(body):
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ingestion.IngestionError("不支持带实体声明的订阅源或站点地图")
    try:
        return ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise ingestion.IngestionError("来源不是有效 XML") from None


def _feed_candidates(body, url):
    root = _xml(body)
    local = lambda name: name.rsplit("}", 1)[-1]
    result = []
    for entry in (node for node in root.iter() if local(node.tag) in {"item", "entry"}):
        values, link = {}, None
        for child in entry:
            key = local(child.tag)
            values[key] = "".join(child.itertext()).strip()
            if key == "link" and child.get("rel", "alternate") == "alternate":
                link = child.get("href") or values[key]
        if link and values.get("title"):
            result.append({"url": urljoin(url, link), "title": ingestion.html_text(values["title"]),
                           "description": ingestion.html_text(values.get("description") or values.get("summary") or "")[:1200],
                           "publisher": values.get("Source") or values.get("source"),
                           "published_at": feed_published_time(values.get("pubDate") or values.get("published"), url)})
        if len(result) >= 100:
            break
    return result


def _sitemap_candidates(body):
    result = []
    for node in _xml(body):
        values = {child.tag.rsplit("}", 1)[-1]: child.text for child in node}
        url = values.get("loc") or ""
        if not re.search(r"/(news|feature)/[^/]+$", urlsplit(url).path):
            continue
        title = re.sub(r"-a[A-Za-z0-9]{8,}$", "", unquote(urlsplit(url).path.rsplit("/", 1)[-1])).replace("-", " ")
        result.append({"url": url, "title": title, "description": "标题由官网文章链接提取，读取原文后核对。",
                       "published_at": None, "title_from_url": True,
                       "source_modified_at": ingestion.published_time(values.get("lastmod"))})
    return result[:1000]


def _walk(value, depth=0):
    if depth > 24:
        return
    if isinstance(value, dict):
        yield value
        for key, child in value.items():
            if key not in {"relatedArticles", "nowTrending", "initialMegaNavMenus", "footer", "menu"}:
                yield from _walk(child, depth + 1)
    elif isinstance(value, list):
        for child in value[:120]:
            yield from _walk(child, depth + 1)


def _article_path(url, source):
    path = urlsplit(url).path.rstrip("/")
    if urlsplit(url).hostname in TYC_HOSTS:
        return bool(re.fullmatch(r"/(?:[a-z0-9-]+/)+[a-z0-9-]+-id\d+\.html", path))
    if source["id"] == "official_arsenal":
        return bool(re.match(r"/(news|feature)/[^/]+-a[A-Za-z0-9]+$", path))
    if source["id"] == "official_liverpool":
        return bool(re.match(r"/news/[^/]+$", path))
    if source["id"] == "official_inter":
        return bool(re.match(r"/en/news/[^/]+$", path))
    if source["id"] == "official_barcelona":
        return bool(re.match(r"/en/(?:football/first-team/)?news/\d+/[^/]+$", path))
    return bool(re.search(r"/(news|article|stories|sport|football)/.+", path))


def _html_candidates(body, url, source):
    soup = BeautifulSoup(body, "html.parser")
    result = []
    for data in ingestion.embedded_json(soup):
        for node in _walk(data):
            title = node.get("title") or node.get("headline")
            href = node.get("url") or node.get("path")
            if not href and node.get("slug") and source["id"] in {"official_inter", "official_liverpool"}:
                href = ("/en/news/" if source["id"] == "official_inter" else "/news/") + node["slug"]
            if not isinstance(title, str) or not isinstance(href, str):
                continue
            link = urljoin(url, href)
            if _article_path(link, source):
                result.append({"url": link, "title": ingestion.clean_text(title),
                               "description": ingestion.html_text(str(node.get("seoDescription") or node.get("description") or node.get("summary") or ""))[:1200],
                               "published_at": ingestion.published_time(node.get("publishedAt") or node.get("datePublished") or node.get("publicationDate") or node.get("date"))})
    container = soup.find("main") or soup
    for junk in container.select("nav, footer, header, aside"):
        junk.decompose()
    for anchor in container.find_all("a", href=True):
        href = urljoin(url, anchor["href"])
        if not _article_path(href, source):
            continue
        title_tag = (anchor.find(re.compile("^h[1-6]$")) or anchor.select_one(".news-hero__title, .thumbnail__title, .content-card__title, [class*='__title']"))
        title = (title_tag or anchor).get_text(" ", strip=True)
        if len(title) >= 12:
            stamp = anchor.find("time", datetime=True)
            result.append({"url": href, "title": title, "description": "", "published_at": ingestion.published_time(stamp.get("datetime")) if stamp else None})
    if not result and not source.get("builtin") and soup.find("article"):
        entry = ingestion.extract_html(body, url, source["name"])[0]
        excerpt = entry.pop("text")[:280]
        result.append({**entry, "description": excerpt})
    return result


def _query_parts(team_id, query):
    value = normalized(query)
    people = entities(value)
    groups = [group for group in TOPICS if any(contains(value, word) for word in group)]
    for alias in TEAM_ALIASES.get(team_id, ()):
        value = value.replace(alias, " ")
    for key in people:
        for alias in ENTITY_ALIASES[key]:
            value = value.replace(normalized(alias), " ")
    for group in groups:
        for word in sorted(group, key=len, reverse=True):
            value = value.replace(word, " ")
    # Translate opponent names as concept groups, keeping them mandatory when
    # present. A Napoli question must not match an unrelated Chelsea preview.
    opponents = [group for group in OPPONENT_ALIASES if group != TEAM_ALIASES.get(team_id)
                 and any(contains(value, alias) for alias in group)]
    groups.extend(opponents)
    for group in opponents:
        for alias in sorted(group, key=len, reverse=True):
            value = value.replace(normalized(alias), " ")
    for phrase in ("值得关注", "最近几天", "帮我", "我想知道", "想知道", "请问", "请你", "说说", "分析", "解释", "介绍", "总结", "整理", "新闻", "重点", "继续", "跟进", "追踪", "最新", "最近", "近况", "消息", "变化", "球队", "情况", "看看", "有什么", "怎么样", "哪些", "目前", "今天", "现在", "有没有", "如何", "了解", "一下", "的", "和", "与"):
        value = value.replace(phrase, " ")
    words = {word for word in re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}", value) if word not in STOP_WORDS}
    return people, groups, words


def _topic_matches(value, group):
    if any(contains(value, word) for word in group):
        return True
    # Football headlines use "relishing [opponent] test" for anticipation of
    # an upcoming fixture; a bare "test" could instead be a medical test.
    return bool(group[0] == "赛前" and re.search(r"\brelishing\b.{0,60}\btest\b", value, re.I))


def relevance(team_id, query, candidate):
    people, groups, words = _query_parts(team_id, query)
    value = " ".join(str(candidate.get(key) or "") for key in ("title", "description", "url", "text"))
    if people and not any(any(contains(value, alias) for alias in ENTITY_ALIASES[key]) for key in people):
        return 0
    group_hits = sum(_topic_matches(value, group) for group in groups)
    word_hits = sum(contains(value, word) for word in words)
    opponent_groups = [group for group in groups if group in OPPONENT_ALIASES]
    if any(not _topic_matches(value, group) for group in opponent_groups):
        return 0
    topics = [group for group in groups if group not in opponent_groups]
    if topics and not any(_topic_matches(value, group) for group in topics):
        return 0
    if words and not word_hits:
        return 0
    return 1 + len(people) * 8 + group_hits * 3 + word_hits * 2


def _index_key(team_id, source):
    location = source.get("index_url") or source["url"]
    digest = hashlib.sha256((source["id"] + "\n" + location).encode()).hexdigest()[:20]
    scope = "shared" if source.get("builtin") and source.get("shared_index") else team_id
    return "news_index:" + scope + ":" + digest


def source_status(store, team_id):
    """Explain source coverage without making a network request."""
    result = []
    for source in source_catalog(store, team_id):
        cache = store.get_meta(_index_key(team_id, source), {}) if hasattr(store, "get_meta") else {}
        health = source_health(store, source)
        result.append({"id": source["id"], "name": source["name"], "url": source["url"],
                       "source_type": "official" if source["id"].startswith("official_") else "media" if source.get("builtin") else "custom",
                       "last_checked": cache.get("last_checked"), "last_success": cache.get("fetched_at"),
                       "last_error": cache.get("last_error") or health.get("last_error_message"),
                       "error_code": health.get("last_error_kind"), "backoff_until": health.get("backoff_until"),
                       "access_review_active": bool(source.get("access_review_active")),
                       "indexed_count": len(cache.get("entries", []))})
    return result


async def _source_index(store, team_id, source, *, force=False):
    from .research_transport import FAILURE_COOLDOWN_SECONDS
    key = _index_key(team_id, source)
    cached = store.get_meta(key, {})
    fetched = _stamp(cached.get("fetched_at"))
    age = (datetime.now(timezone.utc) - fetched).total_seconds() if fetched else float("inf")
    if not force and 0 <= age < INDEX_CACHE_SECONDS and not cached.get("last_error"):
        return copy.deepcopy(cached.get("entries", [])), [], {"state": "cached", "checked_at": cached["fetched_at"]}
    last_attempt = _stamp(cached.get("last_checked"))
    if not force and (source.get("access_review_active") or _source_in_cooldown(store, source)):
        health = source_health(store, source)
        message = ("此前核验该来源受限，复核期限到期后将自动重试。" if source.get("access_review_active") else
                   health.get("last_error_message") or "来源暂时不可用，稍后自动重试。")
        stale = 0 <= age <= STALE_INDEX_SECONDS and bool(cached.get("entries"))
        errors = [source["name"] + "：" + message]
        if stale:
            errors.append(source["name"] + "：暂用此前保存的文章索引，尚未确认是否有更新。")
        return (copy.deepcopy(cached["entries"]) if stale else [], errors,
                {"state": "stale" if stale else "unavailable", "checked_at": cached.get("fetched_at") if stale else cached.get("last_checked"),
                 "error": source_failure(source, ingestion.IngestionError(health.get("last_error_message") or message))})
    if (not force and cached.get("last_error") and last_attempt
            and 0 <= (datetime.now(timezone.utc) - last_attempt).total_seconds() < FAILURE_COOLDOWN_SECONDS):
        stale = 0 <= age <= STALE_INDEX_SECONDS and bool(cached.get("entries"))
        errors = [source["name"] + "：" + cached["last_error"]]
        if stale:
            errors.append(source["name"] + "：暂用此前保存的文章索引，尚未确认是否有更新。")
        return (copy.deepcopy(cached["entries"]) if stale else [], errors,
                {"state": "stale" if stale else "unavailable", "checked_at": cached.get("fetched_at") if stale else cached["last_checked"],
                 "error": source_failure(source, ingestion.IngestionError(cached["last_error"]))})
    checked_at = ingestion.now()
    attempt_started = [None]
    try:
        body, url, _ = await _fetch_shared(store, source.get("index_url") or source["url"], source, force=force,
                                          on_start=lambda started: attempt_started.__setitem__(0, started))
        entries = (_sitemap_candidates(body) if source.get("index_url") else
                   _feed_candidates(body, url) if source["kind"] == "rss" else _html_candidates(body, url, source))
        for entry in entries:
            entry.pop("text", None)
            entry["description"] = str(entry.get("description") or "")[:280]
        if not entries:
            raise ingestion.IngestionError("没有发现可读取的文章，页面结构可能已变化。")
        cached = {"entries": entries, "fetched_at": checked_at, "last_checked": checked_at, "last_error": None}
        store.set_meta(key, cached)
        _record_source_health(store, source)
        return entries, [], {"state": "fresh", "checked_at": checked_at}
    except (ingestion.IngestionError, asyncio.TimeoutError) as exc:
        details = ingestion.source_error_details(exc)
        message = details["message"]
        _record_source_health(store, source, error=exc, attempt_started=attempt_started[0])
    except Exception as exc:
        details = ingestion.source_error_details(exc)
        message = details["message"]
        _record_source_health(store, source, error=exc, attempt_started=attempt_started[0])
    cached.update(last_checked=checked_at, last_error=message)
    store.set_meta(key, cached)
    errors = [source["name"] + "：" + message]
    if 0 <= age <= STALE_INDEX_SECONDS and cached.get("entries"):
        errors.append(source["name"] + "：暂用此前保存的文章索引，尚未确认是否有更新。")
        return copy.deepcopy(cached["entries"]), errors, {"state": "stale", "checked_at": cached["fetched_at"], "error": source_failure(source, ingestion.IngestionError(message))}
    return [], errors, {"state": "unavailable", "checked_at": checked_at, "error": source_failure(source, ingestion.IngestionError(message))}


async def _verify_article_dates(store, team_id, source, entries, *, force=False):
    """Verify only a few matching undated links; retain metadata, never bodies."""
    pending = list({entry["url"]: entry for entry in entries if not entry.get("published_at")}.values())[:3]
    async def verify(entry):
        try:
            body, url, _ = await _fetch_shared(store, entry["url"], source, force=force)
            if article_url(url) != entry["url"]:
                raise ingestion.IngestionError("核实日期时来源跳转到了其他文章，未作为近期消息。")
            article = ingestion.extract_html(body, url, source["name"])[0]
            return entry["url"], article.get("published_at"), None
        except (ingestion.IngestionError, asyncio.TimeoutError) as exc:
            return entry["url"], None, source["name"] + "：" + (str(exc) or "文章日期核实超时")
        except Exception:
            return entry["url"], None, source["name"] + "：文章日期解析未完成，已保留其他可读资料。"
    results = await asyncio.gather(*(verify(entry) for entry in pending))
    verified = {url: stamp for url, stamp, _ in results if stamp}
    if verified:
        for entry in entries:
            if entry["url"] in verified:
                entry["published_at"] = verified[entry["url"]]
        key = _index_key(team_id, source)
        cached = store.get_meta(key, {})
        for entry in cached.get("entries", []):
            try:
                stamp = verified.get(article_url(entry["url"]))
            except (KeyError, ingestion.IngestionError):
                continue
            if stamp:
                entry["published_at"] = stamp
        store.set_meta(key, cached)
    return [error for _, _, error in results if error]


def _search_query(store, team_id, query):
    team = store.get("teams", team_id) or {}
    people, groups, words = _query_parts(team_id, query)
    translated = [next((word for word in group if re.search(r"[a-z]", word)), group[0]) for group in groups]
    translated += [next((word for word in ENTITY_ALIASES[key] if re.search(r"[a-z]", word)), key) for key in people]
    return " ".join([team.get("english_name") or team_id.replace("_", " "), "football", *translated,
                     *(word for word in words if re.fullmatch(r"[a-z0-9 -]+", word))])[:240]


def _publisher_source(url, name=None):
    host = urlsplit(url).hostname or ""
    if host not in TRUSTED_HOSTS or host in {"www.bing.com", "bing.com", "site.api.espn.com"}:
        return None
    siblings = {host, host[4:] if host.startswith("www.") else "www." + host} & TRUSTED_HOSTS
    return {"id": "search_" + host.replace(".", "_"), "name": str(name or host)[:120], "url": "https://" + host,
            "allowed_hosts": sorted(siblings), "kind": "html", "builtin": True}


async def _web_news_index(store, team_id, query, *, force=False):
    """No key required. Search indexes are leads, never article evidence."""
    search = _search_query(store, team_id, query)
    provider = {"id": "bing_news", "name": "公开新闻搜索", "url": "https://www.bing.com/news/search?" + urlencode({"q": search, "format": "rss", "setlang": "en-US", "mkt": "en-US", "cc": "us"}),
                "kind": "rss", "builtin": True}
    entries, errors, status = await _source_index(store, team_id, provider, force=force)
    result, rejected = [], 0
    for raw in entries:
        link = raw.get("url", "")
        parts = urlsplit(link)
        if parts.hostname in {"www.bing.com", "bing.com"}:
            link = dict(parse_qsl(parts.query)).get("url", "")
        try:
            link = article_url(link)
        except (ValueError, ingestion.IngestionError):
            rejected += 1
            continue
        source = _publisher_source(link, raw.get("publisher"))
        if not source:
            rejected += 1
            continue
        result.append({**raw, "url": link, "source_descriptor": source, "source_id": source["id"], "source_name": source["name"]})
    return result, errors, {"id": "bing_news", "name": "公开新闻搜索", **status,
                            "query": search, "allowed_count": len(result), "excluded_untrusted_count": rejected}


async def _espn_news_index(store, team_id, *, force=False):
    """Structured public publisher index, scoped by checked season membership."""
    from . import league_data
    members = [item for item in store.list("league_memberships") if item.get("team_id") == team_id]
    if not members:
        return [], [], {"id": "espn_news", "name": "ESPN 新闻", "state": "unavailable", "checked_at": ingestion.now()}
    member = max(members, key=lambda row: row.get("season_start_year", 0))
    league = store.get("leagues", member["league_id"])
    code, provider_id = (league or {}).get("provider_code"), str(member.get("provider_team_id", ""))
    if not code or not provider_id.isdigit():
        return [], [], {"id": "espn_news", "name": "ESPN 新闻", "state": "unavailable", "checked_at": ingestion.now()}
    key, checked = "espn_news:" + team_id, ingestion.now()
    cached = store.get_meta(key, {})
    stamp = _stamp(cached.get("fetched_at"))
    if not force and stamp and 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() < INDEX_CACHE_SECONDS:
        return cached.get("entries", []), [], {"id": "espn_news", "name": "ESPN 新闻", "state": "cached", "checked_at": cached["fetched_at"]}
    failed_at = _stamp(cached.get("last_checked"))
    if (not force and cached.get("last_error") and failed_at
            and 0 <= (datetime.now(timezone.utc) - failed_at).total_seconds() < ESPN_FAILURE_CACHE_SECONDS):
        return [], ["ESPN 新闻：索引暂不可用"], {"id": "espn_news", "name": "ESPN 新闻", "state": "unavailable", "checked_at": cached["last_checked"]}
    try:
        from .research_transport import shared_request
        url = f"{league_data.API}/apis/site/v2/sports/soccer/{code}/news?team={provider_id}&limit=40"
        payload = await shared_request(store, ("espn_news", url), urlsplit(url).hostname,
            lambda: asyncio.wait_for(league_data.fetch_json(url), 20), force=force)
        rows = []
        for article in payload.get("articles", [])[:60]:
            tags = {str(category.get("teamId") or (category.get("team") or {}).get("id")) for category in article.get("categories", []) if isinstance(category, dict)}
            combined = str(article.get("headline", "")) + " " + str(article.get("description", ""))
            if provider_id not in tags and not any(contains(combined, alias) for alias in TEAM_ALIASES.get(team_id, ())):
                continue
            link = ((article.get("links") or {}).get("web") or {}).get("href")
            source = _publisher_source(link or "", "ESPN 足球")
            if source and article.get("headline"):
                rows.append({"title": str(article["headline"])[:200], "description": str(article.get("description") or "")[:1200],
                             "url": link, "published_at": ingestion.published_time(article.get("published")),
                             "source_descriptor": source, "source_id": source["id"], "source_name": source["name"]})
        store.set_meta(key, {"entries": rows, "fetched_at": checked, "last_checked": checked, "last_error": None})
        return rows, [], {"id": "espn_news", "name": "ESPN 新闻", "state": "fresh", "checked_at": checked}
    except Exception:
        # Coverage retains a failed provider separately from a genuine empty
        # result; do not expose response bodies or URLs that can include keys.
        store.set_meta(key, {"entries": [], "last_checked": checked, "last_error": "索引暂不可用"})
        return [], ["ESPN 新闻：索引暂不可用"], {"id": "espn_news", "name": "ESPN 新闻", "state": "unavailable", "checked_at": checked}


@manual_refresh_scope
async def discover_news(store, team_id, query="", limit=6, *, category="all", max_age_days=None, offset=0, force=False):
    _check_team(store, team_id)
    limit = max(1, min(40, int(limit)))
    offset = max(0, min(500, int(offset)))
    query = str(query or "")[:500]
    if category not in CATEGORIES:
        raise ingestion.IngestionError("新闻分类不存在")
    if max_age_days is not None and (type(max_age_days) is not int or not 1 <= max_age_days <= 365):
        raise ingestion.IngestionError("新闻时间范围须为 1 至 365 天")
    sources = source_catalog(store, team_id)
    semaphore = asyncio.Semaphore(3)

    async def discover(source):
        async with semaphore:
            entries, failures, status = await _source_index(store, team_id, source, force=force)
            clean = []
            for raw in entries:
                entry = copy.deepcopy(raw)
                try:
                    entry["url"] = article_url(_checked_url(entry["url"], _source_hosts(source)))
                except ingestion.IngestionError:
                    continue
                entry.update({"source_name": source["name"], "source_id": source["id"], "builtin": source.get("builtin", False),
                              "title": entry["title"][:200], "team_id": team_id, "mode": "live",
                              "index_state": status["state"], "index_checked_at": status["checked_at"],
                              "categories": article_categories(entry)})
                if (_first_team_entry(entry) and (not source.get("global_feed") or global_feed_mentions_team(team_id, entry))
                        and (category == "all" or category in entry["categories"]) and relevance(team_id, query, entry)):
                    clean.append(entry)
            if max_age_days is not None and source.get("builtin") and source.get("verify_article_dates"):
                failures = [*failures, *await _verify_article_dates(store, team_id, source, clean, force=force)]
            clean = [entry for entry in clean if within_days(entry, max_age_days)]
            return clean, failures, {"id": source["id"], "name": source["name"], **status, "matched_count": len(clean)}

    # Let publisher indexes enter their bounded transport queues first. While
    # they are in flight, overlap optional searches within this invocation only;
    # the original coverage rule still decides whether their results are used.
    publisher_tasks = [asyncio.create_task(discover(source)) for source in sources]
    extra_tasks = []
    def start_extras():
        extra_tasks.extend([asyncio.create_task(_web_news_index(store, team_id, query, force=force)),
                            asyncio.create_task(_espn_news_index(store, team_id, force=force))])
    def needs_extras(batches):
        return bool(query.strip()) or sum(len(entries) for entries, _, _ in batches) < limit + offset
    try:
        await asyncio.sleep(0)
        batches = [task.result() for task in publisher_tasks] if all(task.done() for task in publisher_tasks) else None
        # A complete cached index needs no speculative network requests.
        if batches is None or needs_extras(batches):
            start_extras()
        if batches is None:
            batches = await asyncio.gather(*publisher_tasks)
        extras = await asyncio.gather(*extra_tasks) if needs_extras(batches) else []
    finally:
        for task in [*publisher_tasks, *extra_tasks]:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*publisher_tasks, *extra_tasks, return_exceptions=True)
    if extras:
        for web_entries, errors, web_status in extras:
            web_entries = [{**entry, "team_id": team_id, "mode": "live", "builtin": True,
                            "index_state": web_status["state"], "index_checked_at": web_status["checked_at"],
                            "categories": article_categories(entry)} for entry in web_entries
                           if _first_team_entry(entry) and within_days(entry, max_age_days)
                           and (category == "all" or category in article_categories(entry)) and relevance(team_id, query, entry)]
            batches.append((web_entries, errors, {**web_status, "matched_count": len(web_entries)}))
    unique, errors = {}, []
    for entries, failures, status in batches:
        errors.extend(failures)
        for entry in entries:
            # Structured metadata takes priority over a second anchor rendering.
            previous = unique.get(entry["url"])
            if not previous or (not previous.get("published_at") and entry.get("published_at")):
                unique[entry["url"]] = entry
    def rank(item):
        stamp = _stamp(item.get("published_at")) or _stamp(item.get("source_modified_at"))
        age = (datetime.now(timezone.utc) - stamp).total_seconds() / 86400 if stamp else None
        freshness = 3 if age is not None and 0 <= age <= 3 else 2 if age is not None and 0 <= age <= 14 else 1 if age is None else 0
        # Ordering is by how much this candidate answers the current question,
        # not by which publisher produced it. A club's own site is one voice
        # among several and gets no source-tier weight; only the URL keeps
        # equal-relevance pages in a stable order.
        return (freshness, relevance(team_id, query, item), stamp.timestamp() if stamp else 0,
                item.get("url", ""))
    ranked = sorted(unique.values(), key=rank, reverse=True)
    # Round-robin across sources on the already-ranked order, least-represented
    # first: every source reaches the list before any source gets a second slot.
    # A club sitemap can return dozens of pages in one request and would
    # otherwise occupy almost every slot (Arsenal: 39 of 40), leaving the
    # publisher feeds the owner just added with one result between them.
    by_source = {}
    for item in ranked:
        by_source.setdefault(item["source_id"], []).append(item)
    queues = {source_id: list(items) for source_id, items in by_source.items()}
    order = sorted(queues, key=lambda sid: (len(queues[sid]), sid))
    ordered = []
    while any(queues.values()):
        for source_id in order:
            queue = queues[source_id]
            if queue:
                ordered.append(queue.pop(0))
    selected = ordered[offset:offset + limit]
    # Backfill from the remainder when the round-robin left the page short, so a
    # club with only one working source still fills the requested list.
    if len(selected) < limit:
        selected.extend(ordered[limit:limit + (limit - len(selected))])
    candidates = []
    for entry in selected:
        entry["id"] = "candidate_" + hashlib.sha256((team_id + "\n" + entry["url"]).encode()).hexdigest()[:20]
        prior = store.get("candidates", entry["id"])
        entry["discovered_at"] = ingestion.now()
        entry["spoiler"] = ingestion.potential_spoiler(entry["title"], entry.get("description", ""))
        if prior:
            for key in ("evidence_id", "last_read", "last_error"):
                if key in prior:
                    entry[key] = prior[key]
            if prior.get("last_read"):
                entry["title"] = prior["title"]
                entry["published_at"] = prior.get("published_at") or entry.get("published_at")
                entry["spoiler"] = entry["spoiler"] or prior.get("spoiler", False)
                entry["title_from_url"] = False
        store.put("candidates", entry)
        candidates.append(entry)
    return {"candidates": candidates, "errors": errors, "notice": DISCOVERY_NOTICE,
            "source_errors": unique_source_failures([status["error"] for _, _, status in batches if status.get("error")]),
            "sources": [status for _, _, status in batches], "category": category, "max_age_days": max_age_days,
            "matched_count": len(ranked), "offset": offset, "next_offset": offset + limit if offset + limit < len(ranked) else None,
            "coverage": "unavailable" if all(status["state"] == "unavailable" for _, _, status in batches) else "partial" if errors else "available"}


OPPONENT_ALIASES = (
    ("napoli", "那不勒斯", "拿坡里"), ("atletico madrid", "atlético madrid", "马德里竞技", "马竞"),
    ("real madrid", "皇家马德里", "皇马"), ("chelsea", "切尔西"),
    ("manchester city", "man city", "曼城"), ("manchester united", "man utd", "曼联"),
    ("tottenham", "spurs", "热刺"), ("newcastle", "纽卡斯尔", "纽卡"),
    ("bayern", "拜仁"), ("paris saint-germain", "psg", "巴黎圣日耳曼", "大巴黎"),
    *TEAM_ALIASES.values(),
)


def _validate_match_article(match, article):
    opponent = normalized(match.get("opponent", ""))
    variants = {opponent}
    for group in OPPONENT_ALIASES:
        if any(contains(opponent, alias) for alias in group):
            variants.update(group)
    combined = article["title"] + "\n" + article["text"][:12000]
    if not opponent or not any(contains(combined, alias) for alias in variants if len(alias) > 1):
        raise ingestion.IngestionError("原文未明确提及本场对手，不能自动关联到这场比赛；可先作为球队资料读取。")
    try:
        published = datetime.fromisoformat((article.get("published_at") or "").replace("Z", "+00:00"))
        kickoff = datetime.fromisoformat((match.get("kickoff") or "").replace("Z", "+00:00"))
        delta = (published - kickoff).total_seconds()
    except (ValueError, TypeError):
        raise ingestion.IngestionError("缺少可核对的原文发布时间或开球时间，不能自动关联比赛。") from None
    if not -7 * 86400 <= delta <= 3 * 86400:
        raise ingestion.IngestionError("原文时间超出本场比赛前后范围，不能自动关联比赛。")


def candidate_matches(store, team_id, candidate, match_id):
    """Conservative preview gate; full article validation still runs on read."""
    match = store.get("matches", match_id) if match_id else None
    if (not match or match.get("team_id") != team_id or match.get("mode") != "live"
            or candidate.get("team_id") != team_id or candidate.get("mode") != "live"):
        return False
    try:
        _validate_match_article(match, {"title": candidate.get("title", ""), "text": candidate.get("description", ""), "published_at": candidate.get("published_at")})
        return True
    except ingestion.IngestionError:
        return False


def _readable_record(item):
    return bool(item and not item.get("private") and not item.get("deleted")
                and item.get("visibility") not in {"hidden", "deleted"})


def _read_source(store, item):
    source = next((row for row in source_catalog(store, item["team_id"]) if row["id"] == item.get("source_id")), None)
    if not source and str(item.get("source_id") or "").startswith("search_"):
        source = _publisher_source(item["url"], item.get("source_name"))
    if not source:
        raise ingestion.IngestionError("该文章的来源已删除或停用，请重新查找文章。")
    _checked_url(item["url"], _source_hosts(source))
    return source


def _candidate_context(store, team_id, candidate_id, event_id, match_id):
    _check_team(store, team_id)
    candidate = store.get("candidates", candidate_id)
    if not _readable_record(candidate) or candidate.get("team_id") != team_id or candidate.get("mode") != "live":
        raise ingestion.IngestionError("候选文章不存在，或不属于当前球队；请先查找文章。")
    if candidate.get("evidence_id") and not _readable_record(store.get("evidence", candidate["evidence_id"])):
        raise ingestion.IngestionError("这篇来源已隐藏、删除或设为私有，不能通过重新采集恢复访问。")
    event = store.get("events", event_id) if event_id else None
    if event_id and (not event or event.get("mode") != "live" or event.get("team_id") != team_id):
        raise ingestion.IngestionError("事件不存在，或不属于本球队的真实资料。")
    match = store.get("matches", match_id) if match_id else None
    if match_id and (not match or match.get("mode") != "live" or match.get("team_id") != team_id):
        raise ingestion.IngestionError("比赛不存在，或不属于本球队的真实资料。")
    return candidate, match, _read_source(store, candidate)


async def read_candidate(store, team_id, candidate_id, event_id=None, match_id=None, *, force=False):
    candidate, match, source = _candidate_context(store, team_id, candidate_id, event_id, match_id)
    try:
        cached = store.get("evidence", candidate.get("evidence_id")) if candidate.get("evidence_id") else None
        fetched = _stamp((cached or {}).get("fetched_at"))
        cache_age = (datetime.now(timezone.utc) - fetched).total_seconds() if fetched else float("inf")
        reusable = bool(not force and cached and cached.get("text") and cached.get("team_id") == team_id and cached.get("mode") == "live"
                        and cached.get("source_id") == source["id"] and cached.get("url")
                        and article_url(cached["url"]) == article_url(candidate["url"])
                        and 0 <= cache_age < ARTICLE_CACHE_SECONDS)
        if reusable:
            article = {key: cached.get(key) for key in ("title", "text", "url", "published_at", "images", *ingestion.CONTENT_FIELDS)}
        else:
            body, url, _ = await _fetch_shared(store, candidate["url"], source, force=force)
            article = ingestion.extract_html(body, article_url(url), source["name"])[0]
        current, match, current_source = _candidate_context(store, team_id, candidate_id, event_id, match_id)
        if current.get("url") != candidate.get("url") or current.get("source_id") != candidate.get("source_id"):
            raise ingestion.IngestionError("读取期间来源链接已变化，请重新查找文章。")
        _checked_url(article["url"], _source_hosts(current_source))
        candidate, source = current, current_source
        if not article.get("published_at"):
            # A feed's publication date is valid provenance; sitemap lastmod is not.
            article["published_at"] = candidate.get("published_at")
        if match:
            _validate_match_article(match, article)
        result = ingestion.ingest(store, team_id=team_id, **article, source_name=source["name"], source_id=source["id"],
                                  origin="web", event_id=event_id, spoiler=ingestion.potential_spoiler(article["title"], article["text"]))
        if match:
            with store.atomic():
                current = store.get("matches", match_id)
                if current and current.get("team_id") == team_id and current.get("mode") == "live":
                    current["evidence_ids"] = list(dict.fromkeys([*current.get("evidence_ids", []), result["evidence"]["id"]]))
                    store.put("matches", current)
        candidate.update({"evidence_id": result["evidence"]["id"], "last_read": ingestion.now(), "last_error": None,
                          "published_at": article.get("published_at"), "title": article["title"], "spoiler": result["evidence"]["spoiler"]})
        store.put("candidates", candidate)
        result["cached"] = reusable
        return result
    except (ingestion.IngestionError, asyncio.TimeoutError) as exc:
        message = str(exc) or "来源请求超时"
        current = store.get("candidates", candidate_id)
        if current and current.get("team_id") == team_id:
            store.put("candidates", {**current, "last_error": message})
        raise ingestion.IngestionError(message) from None


@manual_refresh_scope
async def collect_team(store, team_id, query="", limit=4, event_id=None, match_id=None, *, category="all", max_age_days=None, max_attempts=None, force=False):
    limit = max(1, min(40, int(limit)))
    found = await discover_news(store, team_id, query, limit=40 if not query.strip() else min(40, limit + 6), category=category, max_age_days=max_age_days, force=force)
    result = {"added": 0, "unchanged": 0, "cached": 0, "evidence_ids": [], "event_ids": [], "candidates": found["candidates"], "errors": list(found["errors"]), "source_errors": list(found.get("source_errors", [])), "notice": found.get("notice", DISCOVERY_NOTICE), "sources": found.get("sources", [])}
    # Only fetches overlap. ingest remains synchronous and atomic; no database
    # transaction spans an await. Existing candidates remain a fallback.
    candidates = sorted(found["candidates"], key=lambda item: (bool(item.get("last_read")), item.get("last_read") or ""))
    ceiling = min(len(candidates), limit + 3, max_attempts if max_attempts is not None else limit + 3)
    attempts, completed, pending = 0, {}, {}
    async def read(candidate):
        try:
            return await read_candidate(store, team_id, candidate["id"], event_id, match_id, **({"force": True} if force else {}))
        except (ingestion.IngestionError, asyncio.TimeoutError) as exc:
            return {"error": candidate["source_name"] + "：" + ingestion.source_error_details(exc)["message"],
                    "source_error": source_failure({"id": candidate.get("source_id"), "name": candidate["source_name"], "url": candidate.get("url")}, exc)}
        except Exception:
            return {"error": candidate["source_name"] + "：来源解析未完成，已保留其他可读资料。",
                    "source_error": source_failure({"id": candidate.get("source_id"), "name": candidate["source_name"], "url": candidate.get("url")}, ingestion.IngestionError("来源正文解析未完成"))}
    try:
        while True:
            successes = sum("error" not in value for value in completed.values())
            while attempts < ceiling and len(pending) < ARTICLE_CONCURRENCY and successes + len(pending) < limit:
                task = asyncio.create_task(read(candidates[attempts]))
                pending[task] = attempts
                attempts += 1
            if not pending:
                break
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                completed[pending.pop(task)] = task.result()
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    for _, imported in sorted(completed.items()):
        if "error" in imported:
            result["errors"].append(imported["error"])
            result["source_errors"].append(imported["source_error"])
        else:
            result["unchanged" if imported["duplicate"] else "added"] += 1
            result["cached"] += int(imported.get("cached", False))
            result["evidence_ids"].append(imported["evidence"]["id"])
            if imported.get("event"):
                result["event_ids"].append(imported["event"]["id"])
    result["evidence_ids"] = list(dict.fromkeys(result["evidence_ids"]))
    result["event_ids"] = list(dict.fromkeys(result["event_ids"]))
    result["attempted_reads"] = attempts
    result["source_errors"] = unique_source_failures(result["source_errors"])
    result["coverage"] = "partial" if result["errors"] and result["evidence_ids"] else "unavailable" if result["errors"] else found.get("coverage", "available")
    return result


async def read_evidence_source(store, evidence_id, *, force=False):
    """Read a stored link again; stale text is never resurrected from disk."""
    item = store.get("evidence", evidence_id)
    if not _readable_record(item):
        raise ingestion.IngestionError("来源记录不存在，或已隐藏、删除或设为私有。")
    if item.get("origin") not in {"web", "rss"} or not item.get("url") or item.get("fixture_record"):
        return item
    source = _read_source(store, item)
    if item.get("text") and not force:
        return item
    body, url, _ = await _fetch_shared(store, item["url"], source, force=force)
    article = ingestion.extract_html(body, article_url(url), source["name"])[0]
    current = store.get("evidence", evidence_id)
    if not _readable_record(current) or any(current.get(key) != item.get(key) for key in ("team_id", "mode", "source_id", "url")):
        raise ingestion.IngestionError("读取期间来源范围或权限已变化，本次原文未保存。")
    _checked_url(article["url"], _source_hosts(_read_source(store, current)))
    item = current
    # Updating the same provenance record invalidates derived reports only
    # when its actual body/metadata changes. Retrieval time is not a version.
    published_at = article.get("published_at") or item.get("published_at")
    item.update(article)
    item["published_at"] = published_at
    item["last_checked_at"] = ingestion.now()
    item["fingerprint"] = ingestion.fingerprint(article["text"])
    item["spoiler"] = ingestion.potential_spoiler(article["title"], article["text"]) or item.get("manual_spoiler", False)
    store.put("evidence", item)
    return store.get("evidence", evidence_id)
