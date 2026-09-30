"""인증 미들웨어 (SPEC §8, §11).

- 브라우저: 로그인 페이지 → 세션 쿠키 (HttpOnly, SameSite=Strict). 세션은 프로세스 메모리 (단일 워커).
- 스크립트: Authorization: Basic 헤더도 허용. 브라우저 인증 팝업이 뜨지 않도록 WWW-Authenticate는 보내지 않는다.
- 라우터 dependency는 StaticFiles mount에 적용되지 않으므로 반드시 미들웨어로 건다.
- 비교는 hmac.compare_digest. 비밀번호·세션 토큰은 로그에 남기지 않는다.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import logging
import secrets
import time

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("console.auth")

SESSION_COOKIE = "nw_session"
LOGIN_PATH = "/login"
PUBLIC_PATHS = frozenset({"/healthz", LOGIN_PATH, "/api/login", "/style.css", "/img.png"})


def parse_basic(header: str | None) -> tuple[str, str] | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    user, sep, password = decoded.partition(":")
    if not sep:
        return None
    return user, password


def client_ip(scope: Scope) -> str:
    client = scope.get("client")
    return client[0] if client else "-"


class SessionStore:
    """token → (username, 만료 monotonic). 절대 만료. console 재기동 시 전부 사라진다."""

    def __init__(self, ttl_sec: int) -> None:
        self.ttl_sec = ttl_sec
        self._sessions: dict[str, tuple[str, float]] = {}

    def create(self, username: str) -> str:
        self._purge()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = (username, time.monotonic() + self.ttl_sec)
        return token

    def get(self, token: str | None) -> str | None:
        if not token:
            return None
        entry = self._sessions.get(token)
        if entry is None:
            return None
        username, expires = entry
        if time.monotonic() >= expires:
            self._sessions.pop(token, None)
            return None
        return username

    def delete(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def _purge(self) -> None:
        now = time.monotonic()
        for t in [t for t, (_, exp) in self._sessions.items() if exp <= now]:
            del self._sessions[t]


class LoginLimiter:
    """클라이언트 IP별 연속 실패 횟수. max_failures회 연속 실패 → lockout_sec 동안 차단."""

    def __init__(self, max_failures: int, lockout_sec: int) -> None:
        self.max_failures = max_failures
        self.lockout_sec = lockout_sec
        self._state: dict[str, tuple[int, float]] = {}  # ip → (연속 실패, 차단 해제 monotonic)

    def locked_for(self, ip: str) -> int:
        _, until = self._state.get(ip, (0, 0.0))
        remaining = until - time.monotonic()
        return int(remaining) + 1 if remaining > 0 else 0

    def fail(self, ip: str) -> None:
        count, until = self._state.get(ip, (0, 0.0))
        if until and until <= time.monotonic():
            count = 0  # 차단이 풀린 뒤에는 처음부터 센다
        count += 1
        if count >= self.max_failures:
            self._state[ip] = (0, time.monotonic() + self.lockout_sec)
            log.warning("event=login_locked client=%s lockout_sec=%d", ip, self.lockout_sec)
        else:
            self._state[ip] = (count, 0.0)

    def reset(self, ip: str) -> None:
        self._state.pop(ip, None)


class Authenticator:
    def __init__(self, username: str, password: str, session_ttl_sec: int,
                 max_failures: int, lockout_sec: int, cookie_secure: bool) -> None:
        if not username or not password:
            raise RuntimeError("ADMIN_USER and ADMIN_PASSWORD must be set")
        self._user = username.encode()
        self._password = password.encode()
        self.sessions = SessionStore(session_ttl_sec)
        self.limiter = LoginLimiter(max_failures, lockout_sec)
        self.cookie_secure = cookie_secure

    def check(self, username: str, password: str) -> bool:
        # 두 비교를 모두 수행해 사용자명 일치 여부가 응답 시간으로 드러나지 않게 한다.
        user_ok = hmac.compare_digest(username.encode(), self._user)
        pw_ok = hmac.compare_digest(password.encode(), self._password)
        return user_ok and pw_ok

    def set_cookie(self, response, token: str) -> None:
        response.set_cookie(SESSION_COOKIE, token, max_age=self.sessions.ttl_sec, path="/",
                            httponly=True, samesite="strict", secure=self.cookie_secure)

    def clear_cookie(self, response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict",
                               secure=self.cookie_secure)


class AuthMiddleware:
    """순수 ASGI 미들웨어. 인증된 사용자명은 scope["state"]["user"]에 둔다 (requested_by 기록용)."""

    def __init__(self, app: ASGIApp, auth: Authenticator) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        conn = HTTPConnection(scope)
        ip = client_ip(scope)

        user = self.auth.sessions.get(conn.cookies.get(SESSION_COOKIE))
        basic = None
        if user is None:
            basic = parse_basic(conn.headers.get("authorization"))
            if basic is not None:
                wait = self.auth.limiter.locked_for(ip)
                if wait:
                    await _json(429, "too many failed attempts", {"Retry-After": str(wait)})(scope, receive, send)
                    return
                if self.auth.check(*basic):
                    self.auth.limiter.reset(ip)
                    user = basic[0]
                else:
                    self.auth.limiter.fail(ip)

        if user is not None:
            scope.setdefault("state", {})["user"] = user
            await self.app(scope, receive, _no_store(send))
            return

        path = scope["path"]
        log.warning("event=auth_failed path=%s client=%s credentials=%s", path, ip,
                    "basic_invalid" if basic else ("session_invalid" if SESSION_COOKIE in conn.cookies else "missing"))
        if path.startswith("/api/") or scope["method"] not in ("GET", "HEAD"):
            response = _json(401, "authentication required")
        else:
            response = RedirectResponse(LOGIN_PATH, status_code=302)
        await response(scope, receive, send)


def _no_store(send: Send) -> Send:
    """인증이 필요한 응답은 브라우저가 캐시하지 않게 한다.

    캐시되면 로그아웃 후에도 index.html·app.js가 서버 확인 없이 뜬다 (API 401로 곧 로그인 페이지로 가지만).
    """
    async def wrapped(message) -> None:
        if message["type"] == "http.response.start":
            headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
            headers.append((b"cache-control", b"no-store"))
            message = {**message, "headers": headers}
        await send(message)
    return wrapped


def _json(status: int, detail: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status, headers=headers)
