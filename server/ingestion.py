"""First-party bounded HTML/RSS ingestion; no football or search service APIs."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

MAX_BODY = 2 * 1024 * 1024
MAX_ENTRIES = 30


def now():
    return datetime.now(timezone.utc).isoformat()


class IngestionError(ValueError):
    pass


def normalize_url(url: str, *, allow_local=False):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(char) < 32 for char in url):
        raise IngestionError("网址格式不正确")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
        host = parts.hostname
        if parts.scheme not in {"http", "https"} or not host or parts.username is not None or parts.password is not None or "\\" in url:
            raise ValueError()
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        if not host or "%" in host:
            raise ValueError()
        if not allow_local:
            if host in {"localhost", "metadata.google.internal"} or host.endswith((".localhost", ".local", ".internal")):
                raise IngestionError("来源网址不可指向本机、私有网络或云元数据服务")
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address and not address.is_global:
                raise IngestionError("来源网址不可指向本机、私有网络或云元数据服务")
        netloc = f"[{host}]" if ":" in host else host
        if port is not None and port != (443 if parts.scheme == "https" else 80):
            netloc += f":{port}"
        return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, IngestionError):
            raise
        raise IngestionError("仅支持不含用户名和密码的 HTTP/HTTPS 网址") from None


def public_addresses(host: str, port: int):
    try:
        addresses = sorted({result[4][0] for result in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except OSError:
        raise IngestionError("来源域名无法解析，请检查网址") from None
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise IngestionError("来源域名解析到本机或私有网络，已阻止访问")
    return addresses


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address, secure, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address, self.secure = address, secure

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), timeout=self.timeout)
        if self.secure:
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)


def fetch_document(url: str, *, allowed_hosts=None, max_body=MAX_BODY, content_types=("text/", "xml", "xhtml")):
    """Resolve and pin a public address for each hop; never re-resolve in transport."""
    # Fixed first-party fixture adapters may read larger embedded JSON pages.
    # Other callers retain the original 2 MiB/text-only defaults.
    max_body = max(1, min(int(max_body), 8 * 1024 * 1024))
    started = time.monotonic()
    current = normalize_url(url)
    for redirect in range(5):
        if allowed_hosts is not None and urlsplit(current).hostname not in allowed_hosts:
            raise IngestionError("文章或重定向超出了该来源的已允许域名")
        remaining = 20 - (time.monotonic() - started)
        if remaining <= 0:
            raise IngestionError("来源请求超时，请稍后重试")
        parts = urlsplit(current)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addresses = public_addresses(parts.hostname, port)
        connection = _PinnedHTTP(parts.hostname, port, addresses[0], parts.scheme == "https", min(8, remaining))
        try:
            target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
            connection.request("GET", target, headers={"Host": parts.netloc, "User-Agent": "Touchline/1.0 (personal football reader)", "Accept": "text/html,application/rss+xml,application/atom+xml,application/xml,text/xml", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location or redirect == 4:
                    raise IngestionError("来源重定向次数过多或目标缺失")
                current = normalize_url(urljoin(current, location))
                continue
            if not 200 <= response.status < 300:
                raise IngestionError(f"来源返回 HTTP {response.status}，请检查访问权限或稍后重试")
            length = response.getheader("Content-Length", "")
            if length.isdigit() and int(length) > max_body:
                raise IngestionError(f"来源内容超过 {max_body // 1024} KB 上限")
            if response.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
                raise IngestionError("来源忽略压缩协商，暂不支持该压缩内容")
            content_type = response.getheader("Content-Type", "").lower()
            if content_type and not any(value in content_type for value in content_types):
                raise IngestionError("来源未返回 HTML 或 RSS/Atom 文本")
            chunks, total = [], 0
            while True:
                if time.monotonic() - started > 20:
                    raise IngestionError("来源请求超时，请稍后重试")
                chunk = response.read(min(65536, max_body + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_body:
                    raise IngestionError(f"来源内容超过 {max_body // 1024} KB 上限")
            return b"".join(chunks), current, content_type
        except IngestionError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError):
            raise IngestionError("无法安全读取来源：网络、证书或响应异常") from None
        finally:
            connection.close()
    raise IngestionError("来源重定向次数过多")


def published_time(value):
    if not value or not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            timestamp = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    if timestamp.tzinfo is None:
        return None
    return timestamp.astimezone(timezone.utc).isoformat()


def clean_text(value):
    return "\n".join(line for line in (re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()) if line)


def html_text(value):
    soup = BeautifulSoup(value, "html.parser")
    for element in soup(["script", "style", "noscript", "iframe", "svg", "nav", "footer", "header", "form"]):
        element.decompose()
    # Paragraph boundaries carry meaning; inline emphasis and spans do not.
    # Separating every text node produced one-word lines on Inter's website.
    for element in soup.find_all(["p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "br", "tr"]):
        element.insert_before("\n")
        element.insert_after("\n")
    return clean_text(soup.get_text(" "))


def embedded_json(soup):
    """Parse inert page data only; never execute scripts or follow JSON URLs."""
    for script in soup.find_all("script"):
        if script.get("type") not in {"application/json", "application/ld+json"} and script.get("id") != "__NEXT_DATA__":
            continue
        try:
            yield json.loads(script.string or script.get_text())
        except (ValueError, TypeError, RecursionError):
            continue


def _json_articles(value, depth=0):
    if depth > 20:
        return
    if isinstance(value, list):
        for node in value[:100]:
            yield from _json_articles(node, depth + 1)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        types = [types] if isinstance(types, str) else types
        if isinstance(types, list) and any(kind in {"Article", "NewsArticle", "BlogPosting", "ReportageNewsArticle", "SportsNewsArticle"} for kind in types):
            yield value
        # Known server-rendered article roots; do not harvest related cards.
        if isinstance(value.get("articleBody"), (str, list)) and (value.get("title") or value.get("headline")):
            yield value
        if isinstance(value.get("newsArticle"), dict):
            yield value["newsArticle"]
        for key in ("props", "pageProps", "data", "article", "@graph"):
            if key in value:
                yield from _json_articles(value[key], depth + 1)


def _body_blocks(value):
    if isinstance(value, str):
        return html_text(value)
    if isinstance(value, list):
        parts = []
        for node in value:
            if not isinstance(node, dict):
                continue
            if node.get("type") == "TEXT":
                parts.append(html_text(node.get("html") or node.get("innerText") or ""))
            elif node.get("type") == "formattedText":
                parts.append(html_text(node.get("formattedText") or ""))
        return clean_text("\n".join(parts))
    return ""


def extract_html(body, url, source_name):
    soup = BeautifulSoup(body, "html.parser")
    title = soup.find("meta", property="og:title")
    title = title.get("content") if title else None
    title = title or (soup.title.get_text(" ", strip=True) if soup.title else None) or source_name
    date = None
    for attrs in ({"property": "article:published_time"}, {"name": "date"}, {"name": "pubdate"}):
        meta = soup.find("meta", attrs=attrs)
        if meta:
            date = published_time(meta.get("content"))
            if date:
                break
    if not date:
        tag = soup.find("time", datetime=True)
        date = published_time(tag.get("datetime")) if tag else None
    if not date:
        def article_date(value):
            if isinstance(value, list):
                return next((result for node in value if (result := article_date(node))), None)
            if isinstance(value, dict):
                types = value.get("@type", [])
                types = [types] if isinstance(types, str) else types
                if isinstance(types, list) and any(kind in {"Article", "NewsArticle", "BlogPosting", "ReportageNewsArticle", "SportsNewsArticle"} for kind in types):
                    result = published_time(value.get("datePublished"))
                    if result:
                        return result
                return article_date(value.get("@graph", []))
            return None
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                date = article_date(json.loads(script.string or script.get_text()))
            except (ValueError, TypeError, RecursionError):
                continue
            if date:
                break
    structured_text = ""
    for data in embedded_json(soup):
        for node in _json_articles(data):
            body_text = _body_blocks(node.get("articleBody") or node.get("blocks"))
            if body_text and node.get("byline"):
                body_text = html_text(node["byline"]) + "\n" + body_text
            if body_text and len(body_text) > len(structured_text):
                structured_text = body_text
            title = node.get("headline") or node.get("title") or title
            date = date or published_time(node.get("datePublished") or node.get("publicationDate") or node.get("publishedAt"))
    # Publisher body selectors avoid menu, sponsor, related-story contamination.
    container = (soup.select_one('[itemprop="articleBody"]') or soup.select_one('#article-body-blocks')
                 or soup.select_one('.article-body-viewer-selector') or soup.select_one('.article__body')
                 or soup.find("article") or soup.find("main") or soup.body or soup)
    for element in container.select("aside, .related-articles, .related-content, [data-component='RelatedArticles'], [data-spacefinder-type*='RichLink'], [data-spacefinder-type*='Newsletter'], gu-island[name='EmailSignUpWrapper']"):
        element.decompose()
    text = (structured_text or html_text(str(container)))[:30000]
    if len(text) < 30:
        raise IngestionError("未提取到足够正文；页面可能需要登录或 JavaScript，可改为手动导入")
    if len(text) < 1500 and re.search(r"checking your browser|just a moment|enable javascript|access denied|verify you are human|page not found|404 not found|正在加载|请启用.{0,10}javascript", text, re.I):
        raise IngestionError("来源返回了加载或访问验证页面，请改为手动导入正文")
    return [{"title": clean_text(str(title))[:200], "text": text, "url": url, "published_at": date}]


def extract_feed(body, url, source_name):
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise IngestionError("不支持含外部实体或文档类型声明的订阅源")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise IngestionError("订阅源不是有效的 RSS/Atom XML") from None
    local = lambda tag: tag.rsplit("}", 1)[-1]
    if local(root.tag) not in {"rss", "feed", "RDF"}:
        raise IngestionError("没有识别到 RSS/Atom 订阅格式")
    results = []
    for entry in [node for node in root.iter() if local(node.tag) in {"item", "entry"}][:MAX_ENTRIES]:
        values = {}
        link = None
        for child in entry:
            name = local(child.tag)
            content = "".join(child.itertext()).strip()
            if name == "link":
                candidate = child.attrib.get("href") or content
                if not link or child.attrib.get("rel", "alternate") == "alternate":
                    link = candidate
            values[name] = content
        raw = values.get("encoded") or values.get("content") or values.get("description") or values.get("summary") or ""
        text = html_text(raw)[:30000]
        if len(text) < 30:
            continue
        article_url = url
        if link:
            try:
                article_url = normalize_url(urljoin(url, link))
            except IngestionError:
                continue
        results.append({"title": clean_text(values.get("title") or source_name)[:200], "text": text,
                        "url": article_url, "published_at": published_time(values.get("pubDate") or values.get("published"))})
    if not results:
        raise IngestionError("订阅源没有可导入的正文条目（每条至少 30 字符）")
    return results


def fingerprint(text):
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()
    return hashlib.sha256(normalized.encode()).hexdigest()


def potential_spoiler(title, text):
    """Conservative keyword flags, never presented as verified match analysis."""
    return spoiler_sensitive(title + "\n" + text)


_SPOILER_CONTENT = re.compile(
    r"(?<![\d-])\d{1,2}\s*[-–—:]\s*\d{1,2}(?![\d-])|\d{1,2}\s*(?:to|vs?\.?|比)\s*\d{1,2}"
    r"|比分|赛果|战报|赛后|完赛|全场结束|半场结束|击败|战胜|告负|不敌|获胜|取胜|胜利|大胜|惨败|失利|落败|输球|赢球|逆转|绝杀|扳平|领先|落后|进球|失球|破门|零封|点球|红牌|黄牌|梅开二度|帽子戏法|战平|平局|积分|夺冠|晋级|淘汰|出局|亚军|冠军"
    r"|\b(?:match report|full[ -]?time|half[ -]?time|post[ -]?match|final score|first half|second half|clean sheet|red card|yellow card|hat[ -]?trick|league title|cup title|title success|title triumph)\b"
    r"|\b(?:beat|beaten|defeat(?:ed|s)?|win(?:s|ner|ners|ning)?|won|lost|loss(?:es)?|victor(?:y|ies)|comeback|draw|drawn|drew|goals?|scor(?:e|ed|es|er|ers|ing)|conced(?:e|ed|es|ing)|equalis(?:e|ed|er|ers|ing)|equaliz(?:e|ed|er|ers|ing)|penalt(?:y|ies)|sent off|dismissed|booked|brace|rout(?:ed)?|thrashed|edged|overcame|triumph(?:ed)?|points?|runners?[ -]?up|champions|eliminated|knocked out)\b",
    re.I,
)


def spoiler_sensitive(text):
    """Shared conservative detector for headings, snippets and source paths."""
    checked = re.sub(r"\b(?:UEFA[ -])?Champions[ -]League\b", "European competition", str(text or ""), flags=re.I)
    return bool(_SPOILER_CONTENT.search(checked))


def spoiler_title(title):
    return spoiler_sensitive(title)


def safe_evidence_view(item):
    """Return a spoiler-safe copy, never mutate or replace the stored original.

    Only automatically collected web/RSS records are eligible for paragraph
    redaction. A manually marked spoiler and a result-bearing headline remain
    hidden. IDs and source metadata stay intact for the same source citation.
    """
    if not item:
        return None
    # Fixture JSON has numeric score fields that paragraph keyword redaction
    # cannot reliably identify. A result-bearing record is hidden as a whole.
    if item.get("fixture_record") and item.get("spoiler"):
        return None
    if not item.get("spoiler"):
        return copy.deepcopy(item)
    if item.get("origin") not in {"web", "rss"} or item.get("manual_spoiler"):
        return None
    # "Champions League" is a competition name, not a disclosed champion.
    if spoiler_title(item.get("title", "")) or spoiler_sensitive(unquote(urlsplit(item.get("url") or "").path)):
        return None
    raw = str(item.get("text") or "").strip()
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n+", raw) if paragraph.strip()]
    # Legacy extractors separated every inline text node, sometimes one word
    # per line. Such fragments cannot safely serve as independent paragraphs.
    if not paragraphs or sum(len(part) < 24 for part in paragraphs) > len(paragraphs) * .6:
        return None
    safe, removed = [], 0
    for paragraph in paragraphs:
        if re.match(r"Explore more on these topics|Share Reuse this content", paragraph, re.I):
            break
        if re.search(r"\bRead more\b|newsletter promotion|View image in fullscreen", paragraph, re.I):
            continue
        if spoiler_sensitive(paragraph):
            removed += 1
        else:
            safe.append(paragraph)
    text = "\n\n".join(safe)
    if not removed or len(text) < 160 or not any(len(paragraph) >= 80 for paragraph in safe):
        return None
    result = copy.deepcopy(item)
    result.update({"text": text, "spoiler": False, "original_spoiler": True, "spoiler_redacted": True,
                   "redacted_paragraph_count": removed,
                   "redaction_note": "避剧透节选：已隐藏含比分、赛果或比赛过程线索的完整段落。此处仅引用保留下来的原文；不能根据删去的内容推断比赛。"})
    return result


def ingest(store, *, team_id, title, text, url="", source_name="手动导入", published_at=None,
           kind="team", stage=None, spoiler=False, event_id=None, source_id=None, origin="manual"):
    text = clean_text(text)
    digest = fingerprint(text)
    with store.atomic():
        event = store.get("events", event_id) if event_id else None
        if event_id and (not event or event["mode"] != "live" or event["team_id"] != team_id):
            raise IngestionError("追加事件不存在，或不属于当前球队的真实资料")
        evidence_list = store.list("evidence", "live", team_id)
        duplicate = next((item for item in evidence_list if item.get("fingerprint", fingerprint(item["text"])) == digest), None)
        if duplicate:
            parent = next((item for item in store.list("events", "live", team_id) if duplicate["id"] in item["evidence_ids"]), None)
            if spoiler and not duplicate.get("spoiler"):
                duplicate["spoiler"] = True
                store.put("evidence", duplicate)
                for existing in store.list("events", "live", team_id):
                    if duplicate["id"] in existing["evidence_ids"]:
                        existing["spoiler"] = True
                        store.put("events", existing)
                        if parent and parent["id"] == existing["id"]:
                            parent = existing
            return {"evidence": duplicate, "event": parent, "duplicate": True}
        from .events import find_related_event, record_update
        if not event:
            event = find_related_event(store, team_id, title, text, url, published_at)
        timestamp = now()
        evidence = {"id": "ev_" + uuid.uuid4().hex[:16], "mode": "live", "team_id": team_id,
                    "title": title, "text": text, "url": url or "", "source_name": source_name,
                    "published_at": published_at, "fetched_at": timestamp, "source_id": source_id,
                    "origin": origin, "spoiler": spoiler, "fingerprint": digest}
        prior_event = dict(event) if event else None
        if not event:
            event = {"id": "event_" + uuid.uuid4().hex[:16], "mode": "live", "team_id": team_id,
                     "title": title, "summary": text[:260], "change": "新增来源资料，尚未经 AI 解读或独立核验。",
                     "kind": kind, "stage": stage or "来源记录 · 待核实", "importance": "normal", "updated_at": timestamp,
                     "evidence_ids": [], "watching": False, "read": False, "spoiler": spoiler,
                     "timeline": [], "uncertainties": ["来源内容不等于已核实事实；需要对照原文与其他独立证据。"]}
        else:
            event.update({"title": title, "stage": stage or event["stage"], "updated_at": timestamp, "summary": text[:260], "change": "该事件新增一份不同正文的资料，请对照时间线查看。", "read": False,
                          "spoiler": event.get("spoiler", False) or spoiler})
        event["evidence_ids"].append(evidence["id"])
        event["timeline"].append({"at": timestamp, "text": "收录资料：" + title, "evidence_id": evidence["id"]})
        record_update(event, evidence, explicit_stage=stage, prior_event=prior_event)
        store.put("evidence", evidence)
        store.put("events", event)
        return {"evidence": evidence, "event": event, "duplicate": False}


async def refresh_source(store, source_id):
    source = store.get("sources", source_id)
    if not source:
        raise IngestionError("来源不存在")
    source["last_checked"] = now()
    source["last_error"] = None
    store.put("sources", source)
    result = {"added": 0, "unchanged": 0, "errors": []}
    try:
        body, url, content_type = await asyncio.wait_for(asyncio.to_thread(fetch_document, source["url"]), timeout=25)
        parser = extract_feed if source["kind"] == "rss" else extract_html
        entries = parser(body, url, source["name"])
        for entry in entries:
            imported = ingest(store, team_id=source["team_id"], source_name=source["name"], source_id=source_id,
                              origin="rss" if source["kind"] == "rss" else "web",
                              spoiler=potential_spoiler(entry["title"], entry["text"]), **entry)
            result["unchanged" if imported["duplicate"] else "added"] += 1
        source.update({"last_success": now(), "last_added": result["added"], "last_error": None})
    except (IngestionError, asyncio.TimeoutError) as exc:
        error = str(exc) if isinstance(exc, IngestionError) else "来源请求超时，请稍后重试"
        source["last_error"] = error
        result["errors"].append(error)
    except Exception:
        source["last_error"] = "来源解析失败，请检查格式或改为手动导入"
        result["errors"].append(source["last_error"])
    latest = store.get("sources", source_id)
    if latest:
        latest.update({key: source.get(key) for key in ("last_checked", "last_error", "last_success", "last_added")})
        store.put("sources", latest)
    return result


async def refresh_all(store):
    sources = [source for source in store.list("sources", "live") if source.get("enabled")]
    limit = asyncio.Semaphore(3)
    async def refresh(source):
        async with limit:
            return await refresh_source(store, source["id"])
    results = await asyncio.gather(*(refresh(source) for source in sources))
    total = {"added": sum(result["added"] for result in results), "unchanged": sum(result["unchanged"] for result in results),
             "errors": [error for result in results for error in result["errors"]]}
    store.set_meta("last_refresh_live", now())
    return total
