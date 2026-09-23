"""First-party bounded HTML/RSS ingestion; no football or search service APIs."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

MAX_BODY = 2 * 1024 * 1024
MAX_ENTRIES = 30
MAX_ARTICLE_TEXT = 30000
MAX_FEED_FOLLOWUPS = 6

CONTENT_FIELDS = ("content_status", "content_method", "content_note", "text_truncated")


def content_details(item):
    """Describe the supplied visible text; callers must apply spoiler scope first.

    Status reports extraction coverage, never truthfulness or verification. Old
    records receive conservative labels instead of pretending an RSS excerpt is
    a complete article. Do not persist the derived paragraphs alongside text.
    """
    text = str(item.get("text") or "")
    paragraphs = [{"index": index, "text": part.strip()} for index, part in
                  enumerate(re.split(r"\n+", text)) if part.strip()]
    words = len(re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", text))
    status = item.get("content_status")
    if status not in {"full_text", "partial", "summary", "imported"}:
        status = "imported" if item.get("origin") == "manual" else "summary" if item.get("origin") == "rss" else "partial"
    note = item.get("content_note") or {
        "full_text": "已提取页面可读正文；不代表已经独立核实，原站可能另有更新。",
        "partial": "当前保存的是可提取的部分正文，不能据此判断全文没有其他信息。",
        "summary": "当前仅有订阅摘要，原文尚未完整读取。",
        "imported": "用户导入的原文；内容范围与完整性取决于导入材料。",
    }[status]
    if item.get("spoiler_redacted"):
        status = "partial"
        note = item.get("redaction_note") or "当前显示避剧透节选，部分原文已隐藏。"
    return {"content_status": status, "content_method": item.get("content_method") or "legacy",
            "content_note": note, "text_truncated": bool(item.get("text_truncated")),
            "paragraphs": paragraphs, "paragraph_count": len(paragraphs), "word_count": words,
            "reading_minutes": max(1, math.ceil(words / 260)) if text else 0}


def now():
    return datetime.now(timezone.utc).isoformat()


class IngestionError(ValueError):
    pass


class SourceConnectionError(IngestionError):
    """Publisher connection is unavailable, independently of an article path."""


def source_error_details(exc):
    """Public diagnostics contain categories, never raw transport URLs/secrets."""
    text = str(exc).lower()
    status = re.search(r"\bhttp\s+(\d{3})\b", text)
    if isinstance(exc, ssl.SSLCertVerificationError) or "证书校验失败" in text or "certificate_verify_failed" in text:
        return {"code": "tls_certificate", "stage": "tls", "message": "来源证书校验失败，请检查网络或系统代理。"}
    if isinstance(exc, TimeoutError) or "超时" in text or "timed out" in text:
        return {"code": "timeout", "stage": "read", "message": "来源请求超时，请稍后重试。"}
    if "域名无法解析" in text or isinstance(exc, socket.gaierror):
        return {"code": "dns_failed", "stage": "dns", "message": "来源域名无法解析，请检查网络。"}
    if isinstance(exc, ssl.SSLError) or "tls 握手失败" in text:
        return {"code": "tls_error", "stage": "tls", "message": "来源 TLS 握手失败，请检查网络或系统代理。"}
    if isinstance(exc, ConnectionResetError) or "连接被重置" in text:
        return {"code": "connection_reset", "stage": "connect", "message": "来源连接被重置，请稍后重试。"}
    if status:
        code = int(status[1])
        name = "http_forbidden" if code in {401, 403} else "http_rate_limited" if code == 429 else "http_unavailable" if code >= 500 else "http_error"
        return {"code": name, "stage": "http", "http_status": code,
                "message": f"来源返回 HTTP {code}，" + ("请求过于频繁，请稍后重试。" if code == 429 else "暂时无法读取正文。")}
    if "验证页面" in text or "访问验证" in text or "地区限制" in text or "同意页面" in text:
        return {"code": "access_challenge", "stage": "parse", "message": "来源返回了访问验证、同意或地区限制页面，暂时无法读取正文。"}
    if any(word in text for word in ("正文", "html", "xml", "文本", "页面结构")):
        return {"code": "content_unavailable", "stage": "parse", "message": "来源没有返回可确认的新闻正文，页面可能需要登录或 JavaScript。"}
    if isinstance(exc, (SourceConnectionError, OSError)) or any(word in text for word in ("网络", "连接", "系统代理")):
        return {"code": "connection_error", "stage": "connect", "message": "无法连接来源，请检查网络或系统代理。"}
    return {"code": "source_unavailable", "stage": "read", "message": "该来源暂时无法读取，请稍后重试。"}


def source_diagnostics(errors, sources):
    """Rebuild source labels from the current visible scope on every read."""
    visible = {source["id"]: source for source in sources}
    result, seen = [], set()
    for error in errors or []:
        if not isinstance(error, dict):
            continue
        eid = error.get("evidence_id")
        if not isinstance(eid, str) or eid not in visible or eid in seen:
            continue
        source = visible[eid]
        try:
            host = urlsplit(normalize_url(source["url"])).hostname if source.get("url") else ""
        except IngestionError:
            host = ""
        result.append({"evidence_id": eid, "source_name": str(source.get("source_name") or "来源")[:160],
                       "host": host, **source_error_details(IngestionError(str(error.get("message") or "")))})
        seen.add(eid)
    return result


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
        raise SourceConnectionError("来源域名无法解析，请检查网址") from None
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
            raise SourceConnectionError("来源请求超时，请稍后重试")
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
            if str(response.getheader("cf-mitigated", "")).lower() == "challenge":
                raise IngestionError("来源返回了访问验证页面，暂时无法读取正文")
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
                    raise SourceConnectionError("来源请求超时，请稍后重试")
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
        except ssl.SSLCertVerificationError:
            raise SourceConnectionError("来源证书校验失败，请检查网络或系统代理") from None
        except TimeoutError:
            raise SourceConnectionError("来源请求超时，请稍后重试") from None
        except ssl.SSLError:
            raise SourceConnectionError("来源 TLS 握手失败，请检查网络或系统代理") from None
        except ConnectionResetError:
            raise SourceConnectionError("来源连接被重置，请稍后重试") from None
        except (OSError, http.client.HTTPException):
            raise SourceConnectionError("无法安全读取来源：网络、证书或响应异常") from None
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


def _remove_hidden_nodes(soup):
    # Honour explicit visibility declarations without evaluating CSS or JS.
    # Remove parents before extraction so neither their text nor images leak.
    for element in soup.find_all(True):
        if element.attrs is None:
            continue
        style = element.get("style", "")
        if (element.has_attr("hidden") or str(element.get("aria-hidden", "")).lower() == "true"
                or re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!\s*important\s*)?(?:;|$)", style, re.I)):
            element.decompose()


def html_text(value):
    soup = BeautifulSoup(value, "html.parser")
    _remove_hidden_nodes(soup)
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


def _article_location(node):
    value = node.get("url") or node.get("mainEntityOfPage") or node.get("@id")
    if isinstance(value, dict):
        return value.get("@id") or value.get("url")
    return value


def _article_matches(node, url):
    from .research import article_url
    value = _article_location(node)
    try:
        return isinstance(value, str) and article_url(urljoin(url, value)) == article_url(url)
    except ValueError:
        return False


def _primary_article_node(soup, url):
    """Select one page-owned object before using any of its factual fields."""
    nodes, seen = [], set()
    for data in embedded_json(soup):
        for node in _json_articles(data):
            signature = json.dumps(node, sort_keys=True, ensure_ascii=False)
            if signature not in seen:
                seen.add(signature)
                nodes.append(node)
    matched = [node for node in nodes if _article_matches(node, url)]
    if matched:
        # Multiple representations of this same page may differ in completeness.
        return max(matched, key=lambda node: len(_body_blocks(node.get("articleBody") or node.get("blocks"))))
    unbound = [node for node in nodes if not _article_location(node)]
    if len(unbound) == 1:
        return unbound[0]
    titles = [tag.get_text(" ", strip=True) for tag in (soup.title, soup.find("h1")) if tag]
    meta = soup.find("meta", property="og:title")
    if meta and meta.get("content"):
        titles.append(meta["content"])
    normalize = lambda value: re.sub(r"\s+", " ", str(value)).strip().casefold()
    matching_titles = [node for node in unbound if normalize(node.get("headline") or node.get("title"))
                       in {normalize(title) for title in titles}]
    return matching_titles[0] if len(matching_titles) == 1 else None


def _rich_text(value, depth=0):
    """Read the publisher's inert Contentful text, retaining block boundaries."""
    if not isinstance(value, dict) or depth > 24:
        return None
    kind = value.get("nodeType")
    if kind == "text":
        return value.get("value") if isinstance(value.get("value"), str) else None
    if kind not in {"document", "paragraph", "blockquote", "ordered-list", "unordered-list", "list-item",
                    "hyperlink", "entry-hyperlink", "asset-hyperlink", "hr", *[f"heading-{level}" for level in range(1, 7)]}:
        return None
    children = value.get("content", [])
    if not isinstance(children, list):
        return None
    parts = [_rich_text(child, depth + 1) for child in children]
    if any(part is None for part in parts):
        return None
    text = "".join(parts)
    return text if kind.endswith("hyperlink") else text + "\n"


def _inter_article_text(soup, url):
    """Separate Inter's public story components from reader/navigation cards."""
    parts = urlsplit(url)
    if parts.hostname not in {"www.inter.it", "inter.it"} or not parts.path.startswith("/en/news/"):
        return ""
    visible = soup.find("article")
    if not visible:
        return ""
    normalize = lambda text: re.sub(r"\s+", "", text)
    visible_text = normalize(html_text(str(visible)))
    for data in embedded_json(soup):
        root = data.get("props") if isinstance(data, dict) else None
        props = root.get("pageProps") if isinstance(root, dict) else None
        if not isinstance(props, dict):
            continue
        page, components = props.get("page"), props.get("components")
        if (not isinstance(page, dict) or not isinstance(components, list)
                or parts.path.rstrip("/") != "/en/news/" + str(page.get("slug"))
                or page.get("pageType") != "article" or page.get("published") is not True
                or page.get("accessLevel") != "Free"):
            continue
        sections = [clean_text(page["subtitle"])] if isinstance(page.get("subtitle"), str) and page["subtitle"].strip() else []
        valid = True
        for component in components:
            if not isinstance(component, dict):
                valid = False
                break
            kind = component.get("__typename")
            if kind == "TextComponent":
                rich = component.get("text")
                text = _rich_text(rich.get("json")) if isinstance(rich, dict) else None
                if text is None:
                    valid = False
                    break
                if isinstance(component.get("title"), str) and component["title"].strip():
                    sections.append(clean_text(component["title"]))
                sections.append(clean_text(text))
            elif kind == "QuoteComponent":
                if not isinstance(component.get("quote"), str):
                    valid = False
                    break
                sections.append(html_text(component["quote"]))
                if isinstance(component.get("author"), str):
                    sections.append(clean_text(component["author"]))
            elif kind == "EmbedComponent" and not html_text(str(component.get("embedHtml") or "")):
                continue
            elif kind not in {"InlineArticleLinkComponent", "VideoPlayerComponent", "CarouselComponent"}:
                # Unknown components may contain substantive article text.
                # Preserve the visible page instead of silently dropping it.
                valid = False
                break
        if valid and sections and all(normalize(section) in visible_text for section in sections if section):
            return clean_text("\n".join(sections))
    return ""


def safe_image_url(value, source_url):
    """Accept only public HTTPS image metadata; never fetch untrusted images."""
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        candidate = normalize_url(urljoin(source_url, value))
        parsed = urlsplit(candidate)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}:
            return None
        host = parsed.hostname or ""
        if "." not in host or host.endswith((".local", ".localhost", ".internal", ".test", ".invalid")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        if re.search(r"\.(?:svg|html?)(?:$|\?)", parsed.path, re.I):
            return None
        return candidate
    except (ValueError, IngestionError):
        return None


def image_identity(url):
    """Identify the same publisher asset across known resizing/cropping URLs.

    Unknown query parameters remain part of the identity: some CDNs use an id
    or source URL in the query to select entirely different photographs.
    """
    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    if host == "guim.co.uk" or host.endswith(".guim.co.uk"):
        match = re.search(r"/img/media/([a-f0-9]{20,64})/", parts.path, re.I)
        if match:
            return "guim:" + match.group(1).lower()
    transforms = {"width", "height", "w", "h", "quality", "q", "fit", "crop", "rect", "dpr", "auto", "format", "fm", "resize"}
    pairs = sorted((key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                   if key.lower() not in transforms and not key.lower().startswith("utm_"))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(pairs), ""))


def article_image_scope(url):
    """Known landing pages cannot establish ownership of an article image."""
    try:
        parts = urlsplit(normalize_url(url))
    except (ValueError, TypeError):
        return False
    segments = [part for part in parts.path.split("/") if part]
    if len(segments) < 2:
        return False
    if parts.hostname in {"theguardian.com", "www.theguardian.com"}:
        return bool(re.search(r"/\d{4}/[a-z]{3}/\d{1,2}/[^/]+", parts.path, re.I))
    return segments[-1].lower() not in {"news", "latest", "football", "sport", "sports", "all", "index.html"}


_MULTI_TEAM_TITLE = re.compile(
    r"\b(?:round[ -]?up|review|live(?:[ -]?blog)?|as it happened|news digest|football daily)\b"
    r"|综述|综览|汇总|集锦|直播|实时更新", re.I)


def _image_mentions_team(value, team_id):
    """Match catalog identities, never a feed label, a credit or an inferred player."""
    from .club_library import TEAM_ALIASES
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    for value in TEAM_ALIASES.get(team_id, ()):
        alias = unicodedata.normalize("NFKC", value).casefold()
        if re.search(r"[\u3400-\u9fff]", alias):
            if alias in text:
                return True
        elif re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text):
            return True
    return False


def source_images(source, limit=4):
    """Return only attributable, current images from one visible document."""
    if (source.get("private") or source.get("deleted") or source.get("spoiler_redacted")
            or source.get("visibility") in {"hidden", "deleted"} or not article_image_scope(source.get("url"))):
        return []
    # Local import avoids a cycle: research uses ingestion to retrieve pages.
    from .research import article_url
    try:
        document = article_url(source["url"])
    except (ValueError, TypeError):
        return []
    # A roundup can legitimately contain this club while its generic lead
    # montage illustrates entirely different teams. Article membership alone
    # is insufficient for that image: require an explicit headline or caption
    # identity. RSS names such as "Guardian · Barcelona" are routing metadata.
    require_team_caption = bool(_MULTI_TEAM_TITLE.search(str(source.get("title") or ""))) and not _image_mentions_team(source.get("title"), source.get("team_id"))
    results, seen = [], set()
    for image in source.get("images") or []:
        if not isinstance(image, dict) or image.get("evidence_id", source.get("id")) != source.get("id"):
            continue
        if require_team_caption and not _image_mentions_team(image.get("caption"), source.get("team_id")):
            continue
        try:
            if not image.get("source_url") or article_url(image["source_url"]) != document:
                continue
        except (ValueError, TypeError):
            continue
        clean = safe_image_url(image.get("url"), source["url"])
        if not clean or image_identity(clean) in seen:
            continue
        seen.add(image_identity(clean))
        results.append({**image, "url": clean, "evidence_id": source.get("id"), "source_url": source["url"]})
        if len(results) >= limit:
            break
    return results


def extract_images(soup, url, source_name):
    """Article-owned figures and lead metadata; section artwork is not news."""
    if not article_image_scope(url):
        return []
    results, seen, caption_priorities = [], set(), {}
    articles = soup.find_all("article")
    primary = soup.select_one('[itemprop="articleBody"], #article-body-blocks, .article__body, .article-body, .article-content, .story-body, .entry-content')
    if primary is None and len(articles) == 1 and articles[0].find("p"):
        primary = articles[0]
    # A collection can carry NewsArticle JSON for related cards. Accept only
    # nodes explicitly bound to this page, or a single root article body.
    node = _primary_article_node(soup, url)
    nodes = [node] if node and (_article_matches(node, url) or
                               (not _article_location(node) and primary is not None and node.get("articleBody"))) else []
    if primary is None and not nodes:
        return []
    def add(value, caption="", credit="", caption_priority=1):
        clean = safe_image_url(value, url)
        identity = image_identity(clean) if clean else None
        if clean and identity in seen and caption:
            existing = next(row for row in results if image_identity(row["url"]) == identity)
            if caption_priority > caption_priorities.get(identity, 0):
                existing["caption"] = clean_text(str(caption))[:300]
                caption_priorities[identity] = caption_priority
        if clean and identity not in seen and len(results) < 4:
            seen.add(identity)
            caption_priorities[identity] = caption_priority if caption else 0
            results.append({"url": clean, "source_url": url, "caption": clean_text(str(caption))[:300],
                            "credit": clean_text(str(credit or source_name))[:160], "article_image": True})
    for meta in soup.select('meta[property="og:image"], meta[name="twitter:image"]'):
        add(meta.get("content"))
    for node in nodes:
        images = node.get("image") or []
        for image in images if isinstance(images, list) else [images]:
            if isinstance(image, str):
                add(image)
            elif isinstance(image, dict):
                add(image.get("url") or image.get("contentUrl"), image.get("caption", ""), image.get("creditText", ""), caption_priority=2)
    figures = primary.select("figure")[:8] if primary is not None else []
    for figure in figures:
        if figure.find_parent(["aside", "nav"]) or any(re.search(r"related|recommend|promo", " ".join(parent.get("class", [])), re.I) for parent in [figure, *figure.parents]):
            continue
        img = figure.find("img")
        caption = figure.find("figcaption")
        if img:
            srcset = img.get("srcset") or img.get("data-srcset") or ""
            fallback = srcset.split(",")[-1].strip().split(" ")[0] if srcset else None
            add(img.get("src") or img.get("data-src") or img.get("data-original") or fallback,
                caption.get_text(" ", strip=True) if caption else img.get("alt", ""), caption_priority=3 if caption else 1)
    return results


def extract_html(body, url, source_name):
    soup = BeautifulSoup(body, "html.parser")
    _remove_hidden_nodes(soup)
    # Challenge pages can be arbitrarily long. Match their structural markers
    # and leading instructions, not a news article merely discussing a captcha.
    headings = " ".join(tag.get_text(" ", strip=True) for tag in soup.select("title, h1"))
    challenge = r"checking your browser|just a moment|verify you are human|access denied|consent required|before you continue|not available in your (?:country|region)|unavailable in your (?:country|region)|page not found|404 not found|请完成.{0,10}验证|访问验证|地区限制"
    instructions = soup.get_text(" ", strip=True)[:350]
    if (re.search(challenge, headings, re.I)
            or soup.select_one('form#challenge-form, #cf-challenge-running, #challenge-running, form[action*="/cdn-cgi/challenge"]')
            or re.match(r"\s*(?:please )?(?:enable javascript|checking your browser|verify you are human|请启用.{0,10}javascript|正在加载)", instructions, re.I)):
        raise IngestionError("来源返回了访问验证、同意或地区限制页面，暂时无法读取正文")
    images = extract_images(soup, url, source_name)
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
    structured_text = ""
    node = _primary_article_node(soup, url)
    subscriber_only = bool(node and str(node.get("isAccessibleForFree", "")).lower() == "false")
    if node:
        # A paid article's inert metadata must not reveal content beyond the
        # public page preview. Only use the visible HTML in this case.
        structured_text = "" if subscriber_only else _body_blocks(node.get("articleBody") or node.get("blocks"))
        if structured_text and isinstance(node.get("byline"), str):
            structured_text = html_text(node["byline"]) + "\n" + structured_text
        title = node.get("headline") or node.get("title") or title
        date = published_time(node.get("datePublished") or node.get("publicationDate") or node.get("publishedAt")) or date
    if not structured_text and not subscriber_only:
        structured_text = _inter_article_text(soup, url)
    # Prefer publisher article bodies, then semantic containers. Generic page
    # fallbacks remain partial: a long menu is not evidence of a complete story.
    container = soup.select_one('[itemprop="articleBody"], #article-body-blocks, .article-body-viewer-selector, .article__body, .article-body, .article-content, .story-body, .entry-content')
    method = "html_article" if container else "html_fallback"
    container = container or soup.find("article") or soup.find("main") or soup.body or soup
    if container.name == "article":
        method = "html_article"
    for element in container.select("aside, .related-articles, .related-content, .article-related, .newsletter, .social-share, .share-buttons, [role='navigation'], [aria-hidden='true'], [data-component='RelatedArticles'], [data-spacefinder-type*='RichLink'], [data-spacefinder-type*='Newsletter'], gu-island[name='EmailSignUpWrapper']"):
        element.decompose()
    if urlsplit(url).hostname in {"www.tycsports.com", "tycsports.com"}:
        for element in container.select(".alsoReadContainer, .articleComments, .breadcrumbs, #followGoogleBtn"):
            element.decompose()
        for section in container.select("section.containerGrid"):
            if section.select_one(".card"):
                section.decompose()
    raw_text = structured_text or html_text(str(container))
    if structured_text:
        method = "structured_article"
    text = raw_text[:MAX_ARTICLE_TEXT]
    if len(text) < 30:
        raise IngestionError("未提取到足够正文；页面可能需要登录或 JavaScript，可改为手动导入")
    gated = subscriber_only or bool(re.search(r"subscribe to (?:continue|read)|sign in to (?:continue|read)|already a subscriber|unlock (?:this|the) article|suscr[ií]bete para (?:seguir leyendo|leer)|assine para (?:continuar|ler)|订阅后阅读|登录后阅读", text, re.I))
    full = method != "html_fallback" and len(text) >= 500 and not gated and len(raw_text) <= MAX_ARTICLE_TEXT
    note = ("原站仅提供订阅或登录前的可见部分，未绕过访问限制。" if gated else
            "正文超过保存上限，当前保留前 30000 字符。" if len(raw_text) > MAX_ARTICLE_TEXT else
            "已提取页面可读正文；不代表已经独立核实，原站可能另有更新。" if full else
            "仅提取到较短正文或通用页面文本，完整性尚不能确认。")
    return [{"title": clean_text(str(title))[:200], "text": text, "url": url, "published_at": date,
             "content_status": "full_text" if full else "partial", "content_method": method,
             "content_note": note, "text_truncated": len(raw_text) > MAX_ARTICLE_TEXT, "images": images}]


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
            if name in {"content", "encoded", "description", "summary"} and len(child):
                # Atom XHTML bodies carry real paragraph elements, not an
                # escaped HTML string. Serialise the children to keep them.
                content = (child.text or "") + "".join(ElementTree.tostring(node, encoding="unicode") for node in child)
                content = re.sub(r"(<\/?)[A-Za-z0-9_]+:(?=[A-Za-z])", r"\1", content)
            if name == "link":
                candidate = child.attrib.get("href") or content
                if not link or child.attrib.get("rel", "alternate") == "alternate":
                    link = candidate
            values[name] = content
        raw = values.get("encoded") or values.get("content") or values.get("description") or values.get("summary") or ""
        raw_text = html_text(raw)
        text = raw_text[:MAX_ARTICLE_TEXT]
        if len(text) < 30:
            continue
        article_url = url
        if link:
            try:
                article_url = normalize_url(urljoin(url, link))
            except IngestionError:
                continue
        embedded_body = bool(values.get("encoded") or values.get("content"))
        results.append({"title": clean_text(values.get("title") or source_name)[:200], "text": text,
                        "url": article_url, "published_at": published_time(values.get("pubDate") or values.get("published")),
                        "content_status": "partial" if embedded_body else "summary",
                        "content_method": "feed_content" if embedded_body else "feed_summary",
                        "content_note": "订阅源提供的正文片段，尚未与原始文章核对完整性。" if embedded_body else "当前仅有订阅摘要，原文尚未完整读取。",
                        "text_truncated": len(raw_text) > MAX_ARTICLE_TEXT})
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
    if not raw and item.get("body_storage") == "transient" and item.get("safe_view_meta"):
        result = copy.deepcopy(item)
        result.update(item["safe_view_meta"])
        result.update(text="", spoiler=False, original_spoiler=True, spoiler_redacted=True,
                      redaction_note="原文需要重新读取；当前仅保存避剧透来源记录。")
        return result
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
    if result.get("body_hash"):
        result["body_hash"] = hashlib.sha256(text.encode()).hexdigest()
    return result


def ingest(store, *, team_id, title, text, url="", source_name="手动导入", published_at=None,
           kind="team", stage=None, spoiler=False, event_id=None, source_id=None, origin="manual",
           content_status=None, content_method=None, content_note=None, text_truncated=False, images=None):
    text = clean_text(text)
    digest = fingerprint(text)
    with store.atomic():
        event = store.get("events", event_id) if event_id else None
        if event_id and (not event or event["mode"] != "live" or event["team_id"] != team_id):
            raise IngestionError("追加事件不存在，或不属于当前球队的真实资料")
        evidence_list = store.list("evidence", "live", team_id)
        duplicate = next((item for item in evidence_list if item.get("fingerprint", fingerprint(item.get("text", ""))) == digest
                          and (not url or not item.get("url") or item.get("url") == url)), None)
        if duplicate:
            # Repopulate the short-lived body after restart. Reading the same
            # article never changes its source version or editorial provenance.
            duplicate["text"] = text
            if images:
                duplicate["images"] = images
            store.put("evidence", duplicate)
            parent = next((item for item in store.list("events", "live", team_id) if duplicate["id"] in item["evidence_ids"]), None)
            if content_status and url and url == duplicate.get("url") and source_id == duplicate.get("source_id"):
                duplicate.update({"content_status": content_status, "content_method": content_method,
                                  "content_note": content_note, "text_truncated": bool(text_truncated)})
                duplicate.update({key: value for key, value in content_details(duplicate).items() if key != "paragraphs"})
                store.put("evidence", duplicate)
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
                    "origin": origin, "spoiler": spoiler, "fingerprint": digest, "images": images or []}
        evidence.update({"content_status": content_status or ("imported" if origin == "manual" else "partial"),
                         "content_method": content_method or origin, "content_note": content_note,
                         "text_truncated": bool(text_truncated)})
        evidence.update({key: value for key, value in content_details(evidence).items() if key != "paragraphs"})
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


async def follow_feed_articles(entries, source, feed_url):
    """Bounded follow-through; failure keeps a clearly labelled feed excerpt.

    Only the configured feed host is trusted for custom sources. The BBC's
    fixed feed-to-article domains are an explicit publisher adapter. Every
    fetch still resolves and pins public addresses, including redirects.
    """
    hosts = {urlsplit(normalize_url(source["url"])).hostname}
    if urlsplit(feed_url).hostname not in hosts:
        # A feed may itself have redirected; do not silently grant its entries
        # access to a new domain that the user never configured.
        return entries, {"followed": 0, "full_text": 0, "summary_only": len(entries)}
    if hosts == {"feeds.bbci.co.uk"}:
        hosts.update({"www.bbc.co.uk", "www.bbc.com", "bbc.co.uk", "bbc.com"})
    limit = asyncio.Semaphore(3)
    followed = 0

    async def enrich(entry, index):
        nonlocal followed
        article_url = entry.get("url")
        if index >= MAX_FEED_FOLLOWUPS or not article_url or article_url == feed_url:
            return entry
        if urlsplit(article_url).hostname not in hosts:
            return {**entry, "content_note": "当前为订阅摘要；文章链接超出此来源允许的域名，未自动跟读。"}
        async with limit:
            followed += 1
            try:
                body, final_url, _ = await asyncio.wait_for(asyncio.to_thread(fetch_document, article_url, allowed_hosts=hosts), 25)
                if urlsplit(normalize_url(final_url)).hostname not in hosts:
                    raise IngestionError("文章重定向超出了允许域名")
                article = extract_html(body, final_url, source["name"])[0]
                # A landing page or short consent prompt must not replace a
                # richer feed body. Keep the best actually retrieved content.
                if len(article["text"]) > len(entry["text"]):
                    article["published_at"] = article.get("published_at") or entry.get("published_at")
                    article["content_method"] = "rss_follow_" + article["content_method"]
                    return article
                return {**entry, "content_note": "已尝试读取原站，但没有取得比订阅内容更完整的正文；当前保留订阅片段。"}
            except (IngestionError, asyncio.TimeoutError):
                return {**entry, "content_note": "原站正文暂不可读取，当前保留订阅内容；可打开来源或手动导入正文。"}

    enriched = await asyncio.gather(*(enrich(entry, index) for index, entry in enumerate(entries)))
    return enriched, {"followed": followed,
                      "full_text": sum(item.get("content_status") == "full_text" for item in enriched),
                      "summary_only": sum(item.get("content_status") == "summary" for item in enriched)}


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
        if source["kind"] == "rss":
            entries, coverage = await follow_feed_articles(entries, source, url)
            result.update(coverage)
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
