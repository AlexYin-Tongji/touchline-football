"""Private browser/server deployment: explicit origins and revocable sessions."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

COOKIE = "touchline_session"
SESSION_SECONDS = 7 * 24 * 3600
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver"}


def origin_of(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        return (parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80))
    except ValueError:
        return None


@dataclass(frozen=True)
class WebRuntime:
    server_mode: bool
    public_url: str
    password: str

    @classmethod
    def from_env(cls):
        server_mode = os.getenv("TOUCHLINE_SERVER_MODE", "").lower() in {"true", "1", "yes"} or os.getenv("RENDER") == "true"
        hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "")
        public_url = os.getenv("TOUCHLINE_PUBLIC_URL", "").strip().rstrip("/") or (f"https://{hostname}" if hostname else "")
        password = os.getenv("TOUCHLINE_ACCESS_PASSWORD", "")
        if server_mode:
            if len(password) < 16:
                raise RuntimeError("服务器模式必须设置至少 16 位的 TOUCHLINE_ACCESS_PASSWORD。")
            if not origin_of(public_url) or urlsplit(public_url).path not in {"", "/"} or urlsplit(public_url).query or urlsplit(public_url).fragment:
                raise RuntimeError("服务器模式需要完整的 TOUCHLINE_PUBLIC_URL（Render 可自动提供）。")
            if not public_url.startswith("https://") and urlsplit(public_url).hostname not in LOCAL_HOSTS:
                raise RuntimeError("公网网站必须使用 HTTPS；本机容器预览可使用 HTTP。")
        return cls(server_mode, public_url, password)

    @property
    def hosts(self):
        result = ["127.0.0.1", "localhost", "[::1]", "testserver"]
        if self.server_mode:
            result.append(urlsplit(self.public_url).hostname)
            render_host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "")
            if render_host:
                result.append(render_host)
        return result

    @property
    def secure_cookie(self):
        return self.server_mode and self.public_url.startswith("https://")

    def allows_origin(self, origin):
        parsed = origin_of(origin)
        if self.server_mode:
            return parsed is not None and parsed == origin_of(self.public_url)
        return parsed is not None and parsed[1] in LOCAL_HOSTS

    def describe(self):
        return {"server_mode": self.server_mode, "storage": "server" if self.server_mode else "local"}


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    password: str = Field(min_length=1, max_length=4096)


class PrivateAccess:
    def __init__(self, runtime):
        self.runtime = runtime
        self.sessions = {}
        self.attempts = {}

    def authenticated(self, request):
        if not self.runtime.server_mode:
            return True
        token = request.cookies.get(COOKIE, "")
        if len(token) > 100:
            return False
        key = hashlib.sha256(token.encode()).hexdigest()
        expiry = self.sessions.get(key, 0)
        if expiry <= time.time():
            self.sessions.pop(key, None)
            return False
        return True

    def register(self, app):
        @app.get("/api/auth/status")
        async def status(request: Request):
            return {"required": self.runtime.server_mode, "authenticated": self.authenticated(request), **self.runtime.describe()}

        @app.post("/api/auth/login")
        async def login(payload: LoginInput, request: Request):
            if not self.runtime.server_mode:
                return {"authenticated": True}
            now = time.time()
            # Single process deployment: sessions and rate limits never need a
            # shared cache. Restart revokes every session; personal data persists.
            self.attempts = {ip: value for ip, value in self.attempts.items() if value[1] > now}
            ip = request.client.host if request.client else "unknown"
            failures, expires = self.attempts.get(ip, (0, now + 900))
            if failures >= 8:
                raise HTTPException(429, "尝试次数过多，请 15 分钟后再试。", headers={"Retry-After": str(max(1, int(expires - now)))})
            supplied = hashlib.sha256(payload.password.encode()).digest()
            expected = hashlib.sha256(self.runtime.password.encode()).digest()
            if not hmac.compare_digest(supplied, expected):
                if len(self.attempts) >= 2048 and ip not in self.attempts:
                    raise HTTPException(429, "登录暂时繁忙，请稍后再试。")
                self.attempts[ip] = (failures + 1, expires)
                raise HTTPException(401, "访问密码不正确。")
            self.attempts.pop(ip, None)
            self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}
            if len(self.sessions) >= 128:
                self.sessions.pop(min(self.sessions, key=self.sessions.get))
            token = secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = now + SESSION_SECONDS
            response = JSONResponse({"authenticated": True})
            response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=self.runtime.secure_cookie, samesite="strict", path="/")
            return response

        @app.post("/api/auth/logout")
        async def logout(request: Request):
            self.sessions.pop(hashlib.sha256(request.cookies.get(COOKIE, "").encode()).hexdigest(), None)
            response = JSONResponse({"authenticated": not self.runtime.server_mode})
            response.delete_cookie(COOKIE, path="/", httponly=True, secure=self.runtime.secure_cookie, samesite="strict")
            return response
