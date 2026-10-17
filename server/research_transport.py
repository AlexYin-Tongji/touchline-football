"""Per-store in-flight sharing; no successful response or article-body cache."""
from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from functools import wraps
from urllib.parse import urljoin, urlsplit
from urllib.request import getproxies

import httpx

from .ingestion import IngestionError, SourceConnectionError

REQUEST_CONCURRENCY = 4
HOST_CONCURRENCY = 2
FAILURE_COOLDOWN_SECONDS = 30
MAX_FAILURES = 256
_blocking_workers = ContextVar("research_blocking_workers", default=None)
_manual_refresh_started = ContextVar("research_manual_refresh_started", default=None)

# Fixed, reviewed public metadata endpoints only. Neither configured sources
# nor credentials can widen this proxy transport's destinations.
_PORTRAIT_PATHS = {
    "www.thesportsdb.com": {"/api/v1/json/3/searchplayers.php", "/api/v1/json/3/lookupplayer.php"},
    "thesportsdb.com": {"/api/v1/json/3/searchplayers.php", "/api/v1/json/3/lookupplayer.php"},
    "www.fcbarcelona.com": {"/en/football/first-team/players", "/en/football/first-team/players/"},
    "www.tottenhamhotspur.com": {"/teams/mens/squad", "/teams/mens/squad/"},
    "www.juventus.com": {"/en/teams/first-team-men/squad", "/en/teams/first-team-men/squad/"},
    "en.wikipedia.org": {"/w/api.php"},
    "zh.wikipedia.org": {"/w/api.php"},
    "www.wikidata.org": {"/w/api.php"},
    "commons.wikimedia.org": {"/w/api.php"},
}
# A paid TheSportsDB key is presented as ``X-API-KEY`` on the v2 tree, whose
# paths carry a resource name the v1 tree put in the query string. Listing every
# v2 path would mean guessing endpoint names this project does not call, so the
# rule is a *prefix* on the two hosts that own it — and nowhere else. It is still
# a reviewed destination: the host list above is the whole surface, and a
# redirect is checked against the same function.
_ISSUER_PREFIXES = {
    "www.thesportsdb.com": ("/api/v2/json/",),
    "thesportsdb.com": ("/api/v2/json/",),
}


def _path_allowed(host, path):
    if path in _PORTRAIT_PATHS.get(host, ()):
        return True
    return any(path.startswith(prefix) and len(path) > len(prefix) and ".." not in path
               for prefix in _ISSUER_PREFIXES.get(host, ()))


def fetch_reviewed_portrait_document(url, *, allowed_hosts, max_body, headers=None,
                                     content_types=("text/html", "xhtml")):
    """Use the system HTTPS proxy for reviewed public portrait metadata.

    Some proxy applications resolve publishers to 198.18.* fake addresses.
    Those remain forbidden to the pinned transport; the explicit proxy must
    resolve our fixed publisher hostname instead. This function is synchronous
    so existing shielded workers retain their cancellation/capacity accounting.

    ``headers`` carries a keyed API credential. It is never part of the URL —
    a key in a path is quoted in every failure message that mentions the
    address, and those records outlive the request. The pinned HTTP transport
    accepts this argument too, so a keyed read works with or without a proxy.
    """
    from . import ingestion
    hosts = frozenset(allowed_hosts or ())
    if not hosts or not hosts <= _PORTRAIT_PATHS.keys():
        raise IngestionError("球员照片来源地址未获允许。")
    def checked(value):
        current = ingestion.normalize_url(value)
        parts = urlsplit(current)
        if (parts.scheme != "https" or parts.port not in {None, 443}
                or parts.hostname not in hosts or not _path_allowed(parts.hostname, parts.path)):
            raise IngestionError("球员照片来源或重定向地址未获允许。")
        return current
    current = checked(url)
    max_body = max(1, min(int(max_body), 2 * 1024 * 1024))
    proxy = getproxies().get("https")
    if not proxy:
        return ingestion.fetch_document(current, allowed_hosts=hosts, max_body=max_body,
                                        content_types=content_types, headers=headers)
    started = time.monotonic()
    request_headers = {"User-Agent": ingestion.USER_AGENT,
                       "Accept": "text/html,application/json,text/json,text/plain",
                       "Accept-Encoding": "identity",
                       **(headers or {})}
    try:
        # Using only this explicit OS proxy avoids NO_PROXY sending a reviewed
        # host back through intercepted local DNS. Arbitrary hosts never enter.
        with httpx.Client(proxy=proxy, trust_env=False, follow_redirects=False,
                          headers=request_headers) as client:
            for hop in range(5):
                remaining = 20 - (time.monotonic() - started)
                if remaining <= 0:
                    raise SourceConnectionError("来源请求超时，请稍后重试")
                with client.stream("GET", current, timeout=httpx.Timeout(min(8, remaining))) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if hop == 4 or not response.headers.get("location"):
                            raise IngestionError("来源重定向次数过多或目标缺失")
                        current = checked(urljoin(current, response.headers["location"]))
                        continue
                    if not 200 <= response.status_code < 300:
                        raise IngestionError(f"来源返回 HTTP {response.status_code}")
                    if response.headers.get("cf-mitigated", "").lower() == "challenge":
                        raise IngestionError("来源返回了访问验证页面，暂时无法读取正文")
                    content_type = response.headers.get("content-type", "").lower()
                    if not content_type or not any(kind in content_type for kind in content_types):
                        raise IngestionError("来源未返回所需的网页或 JSON 文本")
                    length = response.headers.get("content-length", "")
                    if length.isdigit() and int(length) > max_body:
                        raise IngestionError("来源内容超过读取上限")
                    chunks, total = [], 0
                    for chunk in response.iter_bytes():
                        if time.monotonic() - started > 20:
                            raise SourceConnectionError("来源请求超时，请稍后重试")
                        total += len(chunk)
                        if total > max_body:
                            raise IngestionError("来源内容超过读取上限")
                        chunks.append(chunk)
                    return b"".join(chunks), current, content_type
    except httpx.TimeoutException:
        raise SourceConnectionError("来源请求超时，请稍后重试") from None
    except IngestionError:
        raise
    except (httpx.HTTPError, ValueError):
        # Transport exceptions can include authenticated proxy URLs. Keep the
        # same safe public category as the pinned reader, never raw details.
        raise SourceConnectionError("无法连接来源，请检查网络或系统代理。") from None
    raise IngestionError("来源重定向次数过多")


def manual_refresh_scope(function):
    """One explicit refresh may retry old failures, not failures it just found."""
    @wraps(function)
    async def scoped(*args, **kwargs):
        if not kwargs.get("force") or _manual_refresh_started.get() is not None:
            return await function(*args, **kwargs)
        token = _manual_refresh_started.set(time.monotonic())
        try:
            return await function(*args, **kwargs)
        finally:
            _manual_refresh_started.reset(token)
    return scoped


async def blocking_request(function, *args, timeout, **kwargs):
    """Cancel the reader promptly while tracking the uncancellable worker."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    workers = _blocking_workers.get()
    if workers is not None:
        workers.add(worker)
    # The reader may leave before a thread raises; always retrieve its outcome.
    worker.add_done_callback(lambda task: None if task.cancelled() else task.exception())
    return await asyncio.wait_for(asyncio.shield(worker), timeout)


class RequestCoordinator:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.pending = {}
        self.failures = {}
        self.host_failures = {}
        self.capacity = asyncio.Semaphore(REQUEST_CONCURRENCY)
        self.hosts = {}
        self.draining = set()

    def release_when_idle(self, host_capacity, workers):
        def release():
            self.capacity.release()
            host_capacity.release()

        if not any(not worker.done() for worker in workers):
            release()
            return

        async def drain():
            try:
                await asyncio.gather(*workers, return_exceptions=True)
            finally:
                release()

        # No caller waits for an abandoned synchronous download, but its real
        # network occupancy still counts against both concurrency limits.
        task = asyncio.create_task(drain())
        self.draining.add(task)
        task.add_done_callback(self.draining.discard)

    async def request(self, key, host, factory, *, force=False):
        now = time.monotonic()
        self.failures = {key: value for key, value in self.failures.items() if value[0] > now}
        self.host_failures = {key: value for key, value in self.host_failures.items() if value[0] > now}
        if force:
            started = _manual_refresh_started.get()
            # A standalone explicit read is a new retry boundary. Child tasks
            # of collect/discover inherit the earlier operation boundary.
            started = now if started is None else started
            for failures, identity in ((self.failures, key), (self.host_failures, host)):
                previous = failures.get(identity)
                if previous is not None and previous[2] < started:
                    failures.pop(identity, None)

        def check_failure():
            for failure in (self.failures.get(key), self.host_failures.get(host)):
                if failure and failure[0] > time.monotonic():
                    raise IngestionError(failure[1])

        check_failure()
        entry = self.pending.get(key)
        if entry is None:
            async def perform():
                try:
                    # Acquire the host slot first, so a busy publisher cannot
                    # occupy all global slots while other publishers are ready.
                    host_capacity = self.hosts.setdefault(host, asyncio.Semaphore(HOST_CONCURRENCY))
                    await host_capacity.acquire()
                    try:
                        # A queued article must not pay another connection
                        # timeout after this publisher has already failed.
                        check_failure()
                        await self.capacity.acquire()
                    except BaseException:
                        host_capacity.release()
                        raise
                    workers = set()
                    token = _blocking_workers.set(workers)
                    try:
                        # A sibling can fail while this request waits for a
                        # global slot even though its host slot was available.
                        check_failure()
                        return await factory()
                    finally:
                        _blocking_workers.reset(token)
                        self.release_when_idle(host_capacity, workers)
                except (IngestionError, asyncio.TimeoutError) as exc:
                    message = str(exc) or "来源请求超时，请稍后重试"
                    failed_at = time.monotonic()
                    self.failures[key] = (failed_at + FAILURE_COOLDOWN_SECONDS, message, failed_at)
                    if isinstance(exc, (SourceConnectionError, asyncio.TimeoutError)):
                        self.host_failures[host] = self.failures[key]
                        while len(self.host_failures) > MAX_FAILURES:
                            self.host_failures.pop(next(iter(self.host_failures)))
                    while len(self.failures) > MAX_FAILURES:
                        self.failures.pop(next(iter(self.failures)))
                    raise IngestionError(message) from None
            entry = {"task": asyncio.create_task(perform()), "waiters": 0}
            self.pending[key] = entry
        entry["waiters"] += 1
        try:
            # One cancelled reader must not interrupt another active reader.
            return await asyncio.shield(entry["task"])
        finally:
            entry["waiters"] -= 1
            if not entry["waiters"]:
                self.pending.pop(key, None)
                task = entry["task"]
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def shared_request(store, key, host, factory, *, force=False):
    state = getattr(store, "_research_requests", None)
    if state is None or state.loop is not asyncio.get_running_loop():
        state = RequestCoordinator()
        store._research_requests = state
    return await state.request(key, host, factory, force=force)
