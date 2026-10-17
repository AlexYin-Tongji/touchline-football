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
PUBLICATION_FIELDS = ("published_at", "published_date", "publication_precision")

# Wikimedia answers HTTP 403 "Please respect our robot policy" to any client
# whose User-Agent names only a product. Its policy requires a way to contact
# the operator, and a browser string fares no better because the request does
# not come from a browser. A single constant is used everywhere so the three
# strings that had drifted apart (Touchline/1.0, 2.0 and 7.1) can no longer
# disagree about who is asking.
USER_AGENT = ("Touchline/2.0 (personal football reader; "
              "+https://github.com/AlexYin-Tongji/touchline-football)")


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
    if "来源已删除或停用" in text or "来源已停用" in text:
        return {"code": "source_disabled", "stage": "scope", "message": "该文章的来源已删除或停用，请重新查找文章。"}
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
    """Addresses a hostname resolves to that are safe to connect to.

    A host may resolve to a mix of public and private addresses — CDNs that
    serve Chinese publishers commonly return both, and some answer with an
    IPv6 link-local address alongside a routable one. Rejecting the whole host
    over that mix blocks sources the app can plainly read, so the private
    addresses are dropped and the connection is still pinned to a public one,
    which is what the check exists to guarantee. A host with *no* public
    address remains refused.
    """
    try:
        addresses = sorted({result[4][0] for result in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except OSError:
        raise SourceConnectionError("来源域名无法解析，请检查网址") from None
    if not addresses:
        raise SourceConnectionError("来源域名无法解析，请检查网址") from None
    public = [address for address in addresses if ipaddress.ip_address(address).is_global]
    if not public:
        raise IngestionError("来源域名解析到本机或私有网络，已阻止访问")
    return public


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, address, secure, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address, self.secure = address, secure

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), timeout=self.timeout)
        if self.secure:
            self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)


def fetch_document(url: str, *, allowed_hosts=None, max_body=MAX_BODY, content_types=("text/", "xml", "xhtml"),
                   headers=None):
    """Resolve and pin a public address for each hop; never re-resolve in transport.

    ``headers`` carries credentials for keyed APIs. They are sent to the pinned
    address only, are never logged, and are not part of the URL, so a stored
    request key cannot leak them.
    """
    # Fixed first-party fixture adapters may read larger embedded JSON pages.
    # Other callers retain the original 2 MiB/text-only defaults.
    max_body = max(1, min(int(max_body), 8 * 1024 * 1024))
    started = time.monotonic()
    current = normalize_url(url)
    origin = urlsplit(current)
    pinned_origin = (origin.scheme, origin.hostname, origin.port or 443)
    if headers:
        if origin.scheme != "https":
            raise IngestionError("带凭据的来源请求必须使用 HTTPS。")
        for key, value in headers.items():
            if (not isinstance(key, str) or not isinstance(value, str)
                    or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)
                    or key.lower() in {"host", "connection", "content-length", "transfer-encoding"}):
                raise IngestionError("来源请求头格式不正确。")
    for redirect in range(5):
        hop = urlsplit(current)
        if headers and (hop.scheme, hop.hostname, hop.port or (443 if hop.scheme == "https" else 80)) != pinned_origin:
            raise IngestionError("带凭据的来源重定向不得离开原 HTTPS 地址。")
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
            request_headers = {"Host": parts.netloc, "User-Agent": USER_AGENT,
                               "Accept": "text/html,application/rss+xml,application/atom+xml,application/xml,text/xml,application/json",
                               "Accept-Encoding": "identity"}
            request_headers.update(headers or {})
            connection.request("GET", target, headers=request_headers)
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


def publication_metadata(value, *, language=""):
    """Preserve the precision declared by the publisher; never invent a zone."""
    instant = published_time(value)
    if instant:
        return {"published_at": instant, "published_date": instant[:10], "publication_precision": "instant"}
    result = {"published_at": None, "published_date": None, "publication_precision": None}
    if not isinstance(value, str) or len(value) > 160:
        return result
    value = value.strip()
    year = month = day = None
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?", value)
    if iso:
        year, month, day = map(int, iso.groups())
    else:
        numeric = re.search(r"(?<!\d)(\d{1,2})([./])(\d{1,2})\2(\d{4})(?!\d)", value)
        if numeric:
            first, _, second, declared_year = numeric.groups()
            if int(first) > 12 or language.lower().split("-")[0] in {"de", "fr", "es", "it", "pt"} or language.lower() == "en-gb":
                day, month, year = int(first), int(second), int(declared_year)
        if year is None:
            months = (
                ("january", "jan", "janvier", "janv", "januar", "enero", "gennaio", "janeiro"),
                ("february", "feb", "février", "fevrier", "févr", "februar", "febrero", "febbraio", "fevereiro"),
                ("march", "mar", "mars", "märz", "maerz", "marzo", "março"),
                ("april", "apr", "avril", "avr", "abril", "aprile"),
                ("may", "mai", "mayo", "maggio", "maio"),
                ("june", "jun", "juin", "juni", "junio", "giugno", "junho"),
                ("july", "jul", "juillet", "juil", "juli", "julio", "luglio", "julho"),
                ("august", "aug", "août", "aout", "agosto"),
                ("september", "sept", "sep", "septembre", "septiembre", "settembre", "setembro"),
                ("october", "oct", "octobre", "oktober", "okt", "octubre", "ottobre", "outubro"),
                ("november", "nov", "novembre", "noviembre", "novembro"),
                ("december", "dec", "décembre", "decembre", "dezember", "dez", "diciembre", "dicembre", "dezembro"),
            )
            for number, aliases in enumerate(months, 1):
                names = "|".join(re.escape(alias) for alias in aliases)
                found = re.search(r"(?<!\d)(\d{1,2})\.?\s+(?:de\s+)?(?:" + names + r")\.?\s+(?:de\s+)?(\d{4})(?!\d)", value, re.I)
                if found:
                    day, year = map(int, found.groups())
                    month = number
                    break
    if year is not None:
        try:
            result.update(published_date=datetime(year, month, day).date().isoformat(), publication_precision="date")
        except (TypeError, ValueError):
            pass
    return result


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
        if (isinstance(value.get("articleBody"), (str, list)) or isinstance(value.get("contentBlocks"), list)) and (value.get("title") or value.get("headline")):
            yield value
        if isinstance(value.get("newsArticle"), dict):
            yield value["newsArticle"]
        for key in ("props", "pageProps", "data", "article", "news", "newsArticle", "content", "attributes", "entry", "mainEntity", "@graph"):
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
        if isinstance(value, str):
            return article_url(urljoin(url, value)) == article_url(url)
        slug = node.get("slug")
        return isinstance(slug, str) and bool(slug) and unquote(urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1] == slug
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
    # A declared slug is an identity even without an absolute canonical URL.
    # A mismatching identity must never become the sole "unbound" fallback.
    unbound = [node for node in nodes if not _article_location(node) and not node.get("slug")]
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
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), _path_without_size(parts.path), urlencode(pairs), ""))


# Publishers serve one photograph under several generated sizes, sometimes in
# the query and sometimes in a path segment. Only the query rule existed, so one
# photo could occupy all four illustration slots.
_SIZE_SEGMENT = re.compile(
    r"^(?:alternates/)?[a-z]?\d{2,4}[a-z]?$"
    r"|^(?:[a-z]{1,6}_)?\d{2,4}x\d{2,4}[a-z]?$"
    r"|^s\d{2,4}[a-z]?$"
    r"|^(?:[a-z]{1,2}_)?(?:preview|landscape|portrait|square|thumbnail|thumb|large|small|medium"
    r"|xl|xxl|xxlarge|big|full|original)$", re.I)
# The same generated size is also embedded in the file name itself:
# ``img_1200x721.jpg``, ``pic-190x114.jpg``, ``s1200d/pic.jpg``.
_NAME_SIZE = re.compile(r"(?:^|[-_])(?:[a-z]?\d{2,4}x\d{2,4}|s\d{2,4}[a-z]?|\d{2,4}d)(?=$|[-_.])", re.I)
def _strip_name_size(name):
    stem, dot, ext = name.rpartition(".")
    if not dot:
        return name
    return _NAME_SIZE.sub("", stem) + dot + ext


def _path_without_size(path):
    """Drop generated-size segments, never an article's own words.

    ``ALTERNATES/s1200d/pic.jpg`` and ``upload/xl_landscape/pic.jpg`` are the
    same photograph; ``news/a.jpg`` and ``news/b.jpg`` are not. A segment is
    only removed when it is *entirely* a size token, and only from the last
    three segments where publishers put them.
    """
    if not path:
        return path
    segments = path.split("/")
    keep = segments[:-4]
    tail = []
    for part in segments[-4:]:
        if _SIZE_SEGMENT.match(part):
            continue
        tail.append(_strip_name_size(part))
    return "/".join([*keep, *tail])


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
_MULTI_TOPIC_TITLE = re.compile(
    r"\b(?:gossip|talking points|round[ -]?up|news digest|football daily|transfer (?:rumou?rs|round[ -]?up))\b"
    r"|流言汇总|传闻汇总|转会汇总|新闻汇总|今日流言", re.I)


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
    title = str(source.get("title") or "")
    # A roundup needs an explicit identity for the picture. When the headline
    # itself names this club, that identity already holds for the page's own
    # lead image — demanding a caption on top of it removed the image from every
    # roundup whose og:image is captionless, which is most of them.
    title_names_team = _image_mentions_team(title, source.get("team_id"))
    require_team_caption = (not title_names_team and
                            (bool(_MULTI_TOPIC_TITLE.search(title)) or bool(_MULTI_TEAM_TITLE.search(title))))
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


def _figure_sources(img):
    """Every URL a ``<img>`` offers, real photographs before placeholders.

    ``add`` de-duplicates, so offering several is cheap; offering the wrong one
    first is what previously cost the whole figure.
    """
    srcset = img.get("srcset") or img.get("data-srcset") or ""
    scaled = [part.strip().split(" ")[0] for part in srcset.split(",") if part.strip()]
    ordered = [img.get("data-src"), img.get("data-original"), img.get("data-lazy-src"),
               img.get("data-srcset") and (scaled[-1] if scaled else None),
               img.get("src"), scaled[-1] if scaled else None]
    seen = set()
    for value in ordered:
        if not isinstance(value, str) or not value or value in seen:
            continue
        if value.startswith("data:") or value.startswith("blob:"):
            continue
        seen.add(value)
        yield value


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
    # A page whose body sits outside the recognised containers still declares
    # its lead image in <head>. Returning early here threw that away and left
    # the article permanently image-less.
    declared = soup.select('meta[property="og:image"], meta[property="og:image:secure_url"], '
                           'meta[property="og:image:url"], meta[name="og:image"], '
                           'meta[name="twitter:image"], meta[name="twitter:image:src"]')
    if primary is None and not nodes and not declared:
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
    for meta in declared:
        add(meta.get("content"))
    for node in nodes:
        images = node.get("image") or []
        for image in images if isinstance(images, list) else [images]:
            if isinstance(image, str):
                add(image)
            elif isinstance(image, dict):
                add(image.get("url") or image.get("contentUrl"), image.get("caption", ""), image.get("creditText", ""), caption_priority=2)
    # Without a recognised body, a <figure> with a caption is still an
    # article-owned picture: the caption is the page's own claim that this
    # image belongs to the story. Requiring a container for it was stricter
    # than the rule for og:image, and it left many pages with no picture at all.
    scope = primary if primary is not None else soup.body or soup
    figures = scope.select("figure")[:8]
    for figure in figures:
        if figure.find_parent(["aside", "nav"]) or any(re.search(r"related|recommend|promo", " ".join(parent.get("class", [])), re.I) for parent in [figure, *figure.parents]):
            continue
        img = figure.find("img")
        caption = figure.find("figcaption")
        if img:
            # Lazy-loading pages put a placeholder in ``src`` and the real
            # photograph in ``data-src``/``srcset``. Testing each candidate in
            # turn — rather than string-OR-ing them — is what lets the real one
            # win: a ``data:`` placeholder is non-empty, so the old chain never
            # reached the attribute holding the image.
            caption_text = caption.get_text(" ", strip=True) if caption else img.get("alt", "")
            for candidate in _figure_sources(img):
                add(candidate, caption_text, caption_priority=3 if caption else 1)
    return results


def _auxiliary_publication_node(tag, scope=None, heading=None):
    for parent in (tag, *tag.parents):
        if parent is scope:
            break
        if (parent.name in {"nav", "aside", "footer"}
                or parent.get("role") in {"navigation", "contentinfo"}
                or re.search(r"related|live[-_]?news|(?:^|[-_ ])(?:card|teaser)(?:$|[-_ ])",
                             " ".join(parent.get("class", [])), re.I)):
            return True
        if (heading is not None and parent.name in {"article", "section"}
                and not any(ancestor is parent for ancestor in heading.parents)):
            # Semantic recommendation cards need no CSS class to identify them
            # as a different article from the page's primary headline.
            return True
        if parent.name == "header" and not parent.find_parent("article"):
            return True
    return False


def _header_publication(soup, *, language=""):
    """Read only explicit publication labels around this page's headline."""
    heading = next((tag for tag in soup.find_all("h1") if not _auxiliary_publication_node(tag)), None)
    if not heading:
        articles = [tag for tag in soup.find_all("article") if not _auxiliary_publication_node(tag)]
        if len(articles) == 1:
            for tag in articles[0].find_all("time", datetime=True):
                if _auxiliary_publication_node(tag, articles[0]):
                    continue
                metadata = publication_metadata(tag.get("datetime"), language=language)
                if metadata["published_date"]:
                    return metadata
        return publication_metadata(None)
    for scope in list(heading.parents)[:4]:
        if scope.name in {"body", "html"}:
            break
        prefix_parts = []
        for node in scope.descendants:
            if node is heading:
                break
            if isinstance(node, str) and node.parent and not _auxiliary_publication_node(node.parent, scope, heading):
                prefix_parts.append(str(node).strip())
        prefix = " ".join(filter(None, prefix_parts)).strip()
        if prefix and len(prefix) <= 160:
            metadata = publication_metadata(prefix, language=language)
            if metadata["published_date"]:
                return metadata
        for tag in scope.find_all(["time", "span", "p", "small", "div"]):
            classes = " ".join(tag.get("class", []))
            if not (tag.name == "time" or re.search(r"published|publication|(?:^|[-_])date|title[-_]overline", classes, re.I)):
                continue
            if _auxiliary_publication_node(tag, scope, heading):
                continue
            value = tag.get("datetime") or tag.get("content") or " ".join(
                str(part).strip() for part in tag.descendants
                if isinstance(part, str) and part.parent
                and not _auxiliary_publication_node(part.parent, scope, heading))
            metadata = publication_metadata(value, language=language)
            if metadata["published_date"]:
                return metadata
        if scope.name in {"article", "main"}:
            break
    return publication_metadata(None)


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
    language = soup.html.get("lang", "") if soup.html else ""
    publication = publication_metadata(None)
    for attrs in ({"property": "article:published_time"}, {"name": "date"}, {"name": "pubdate"}):
        meta = soup.find("meta", attrs=attrs)
        if meta:
            publication = publication_metadata(meta.get("content"), language=language)
            if publication["published_date"]:
                break
    structured_text = ""
    source_scope = ""
    section = soup.find("meta", property="article:section")
    if section:
        source_scope = clean_text(str(section.get("content") or ""))[:160]
    node = _primary_article_node(soup, url)
    subscriber_only = bool(node and str(node.get("isAccessibleForFree", "")).lower() == "false")
    if node:
        category = node.get("mainCategory") or node.get("articleSection") or node.get("category")
        if isinstance(category, dict):
            category = category.get("name") or category.get("title") or category.get("label")
        if isinstance(category, str):
            source_scope = clean_text(category)[:160]
        # A paid article's inert metadata must not reveal content beyond the
        # public page preview. Only use the visible HTML in this case.
        structured_text = "" if subscriber_only else _body_blocks(node.get("articleBody") or node.get("blocks"))
        if structured_text and isinstance(node.get("byline"), str):
            structured_text = html_text(node["byline"]) + "\n" + structured_text
        title = node.get("headline") or node.get("title") or title
        declared = publication_metadata(node.get("datePublished") or node.get("publicationDate") or node.get("publishedAt"), language=language)
        if declared["published_date"]:
            publication = declared
    if not publication["published_date"]:
        publication = _header_publication(soup, language=language)
    if not structured_text and not subscriber_only:
        structured_text = _inter_article_text(soup, url)
    # Prefer publisher article bodies, then semantic containers. Generic page
    # fallbacks remain partial: a long menu is not evidence of a complete story.
    container = soup.select_one('[itemprop="articleBody"], #article-body-blocks, .article-body-viewer-selector, .article__body, .article-body, .article-content, .story-body, .entry-content, .articolo .text, .article .text')
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
    description = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
    scope_intro = str(description.get("content") or "")[:800] if description else ""
    if method != "html_fallback":
        scope_intro += " " + "\n".join(text.splitlines()[:3])[:800]
    else:
        heading = soup.find("h1")
        if heading:
            following_ids = {id(paragraph) for paragraph in heading.find_all_next("p", limit=6)}
            for parent in list(heading.parents)[:6]:
                if parent.name in {"body", "html"}:
                    break
                paragraphs = [paragraph for paragraph in parent.find_all("p")
                              if not paragraph.find_parent(["nav", "aside", "footer"])]
                following = [paragraph.get_text(" ", strip=True) for paragraph in paragraphs
                             if id(paragraph) in following_ids]
                following = [paragraph for paragraph in following if len(paragraph) >= 40]
                if following:
                    scope_intro += " " + " ".join(following[:2])[:800]
                    break
    if len(text) < 30:
        raise IngestionError("未提取到足够正文；页面可能需要登录或 JavaScript，可改为手动导入")
    gated = subscriber_only or bool(re.search(r"subscribe to (?:continue|read)|sign in to (?:continue|read)|already a subscriber|unlock (?:this|the) article|suscr[ií]bete para (?:seguir leyendo|leer)|assine para (?:continuar|ler)|订阅后阅读|登录后阅读", text, re.I))
    full = method != "html_fallback" and len(text) >= 500 and not gated and len(raw_text) <= MAX_ARTICLE_TEXT
    note = ("原站仅提供订阅或登录前的可见部分，未绕过访问限制。" if gated else
            "正文超过保存上限，当前保留前 30000 字符。" if len(raw_text) > MAX_ARTICLE_TEXT else
            "已提取页面可读正文；不代表已经独立核实，原站可能另有更新。" if full else
            "仅提取到较短正文或通用页面文本，完整性尚不能确认。")
    return [{"title": clean_text(str(title))[:200], "text": text, "url": url, "source_scope": source_scope,
             "_scope_intro": scope_intro.strip()[:1600], "_article_owned": bool(node or soup.find("h1") or method != "html_fallback"), **publication,
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
        # The feed already declares its lead image in every common dialect.
        # Reading only the article page threw that away whenever the followed
        # page was not longer than the feed body, or failed to load at all.
        feed_images = []
        for node in entry.iter():
            name = local(node.tag)
            if name in {"content", "thumbnail"}:
                candidate = node.attrib.get("url") or node.attrib.get("href")
                kind = str(node.attrib.get("type") or node.attrib.get("medium") or "image").lower()
                if candidate and kind.startswith("image"):
                    feed_images.append(candidate)
            elif name == "enclosure":
                candidate = node.attrib.get("url")
                if candidate and str(node.attrib.get("type") or "").lower().startswith("image"):
                    feed_images.append(candidate)
            elif name == "image" and node is not entry:
                candidate = node.attrib.get("href") or "".join(node.itertext()).strip()
                if candidate.startswith(("http://", "https://")):
                    feed_images.append(candidate)
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
        declared = [{"url": value, "source_url": url, "caption": "", "credit": source_name, "article_image": True}
                    for value in feed_images]
        results.append({"title": clean_text(values.get("title") or source_name)[:200], "text": text,
                        "url": article_url, "images": declared,
                        "published_at": published_time(values.get("pubDate") or values.get("published")),
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
           published_date=None, publication_precision=None,
           kind="team", stage=None, spoiler=False, event_id=None, source_id=None, origin="manual",
           content_status=None, content_method=None, content_note=None, text_truncated=False, images=None, source_scope="", _scope_intro="", _article_owned=False):
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
                if published_date:
                    duplicate.update(published_date=published_date, publication_precision=publication_precision)
                if _article_owned:
                    duplicate["article_body_bound"] = True
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
        if published_date:
            evidence.update(published_date=published_date, publication_precision=publication_precision)
        if source_scope:
            evidence["source_scope"] = clean_text(str(source_scope))[:160]
        if _article_owned:
            evidence["article_body_bound"] = True
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
