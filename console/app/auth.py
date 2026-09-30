"""인증 · 권한 미들웨어 (SPEC §8, §11).

- 브라우저: 로그인 페이지 → 세션 쿠키 (HttpOnly, SameSite=Strict). 세션은 프로세스 메모리 (단일 워커).
- 스크립트: Authorization: Basic 헤더도 허용. 브라우저 인증 팝업이 뜨지 않도록 WWW-Authenticate는 보내지 않는다.
- 라우터 dependency는 StaticFiles mount에 적용되지 않으므로 반드시 미들웨어로 건다.
- 비밀번호는 scrypt 해시로만 보관한다. 비밀번호·해시·세션 토큰은 로그에 남기지 않는다.
- 역할: admin(전체), monitor(조회 전용, 기본 차단 목록 방식).

해시 생성:  python -m app.auth hash
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import re
import secrets
import sys
import time
from dataclasses import dataclass, field

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("console.auth")

# ---------------------------------------------------------------- 비밀번호 해시 (scrypt)

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN = 2 ** 14, 8, 1, 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str, salt: bytes | None = None) -> str:
    """scrypt:N:r:p:salt:hash (base64url). '$'가 없어 .env·compose 변수 치환에 걸리지 않는다."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                        dklen=SCRYPT_DKLEN, maxmem=_SCRYPT_MAXMEM)
    return f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}:{_b64e(salt)}:{_b64e(dk)}"


def parse_hash(encoded: str) -> tuple[int, int, int, bytes, bytes]:
    """형식이 틀리거나 파라미터가 비정상(서비스 거부 유발)이면 ValueError."""
    parts = encoded.split(":")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise ValueError("expected scrypt:N:r:p:salt:hash")
    n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
    if not (2 ** 10 <= n <= 2 ** 17 and n & (n - 1) == 0 and 1 <= r <= 16 and 1 <= p <= 4):
        raise ValueError("scrypt parameters out of range")
    salt, dk = _b64d(parts[4]), _b64d(parts[5])
    if len(salt) < 8 or len(dk) < 16:
        raise ValueError("salt or hash too short")
    return n, r, p, salt, dk


def verify_password(password: str, encoded: str) -> bool:
    n, r, p, salt, expected = parse_hash(encoded)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=len(expected), maxmem=_SCRYPT_MAXMEM)
    return hmac.compare_digest(dk, expected)


# 없는 사용자명에도 같은 비용을 치르게 하는 더미 해시 (계정 존재 여부가 응답 시간으로 드러나지 않게)
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


# ---------------------------------------------------------------- 계정 · 역할

ROLE_ADMIN = "admin"
ROLE_MONITOR = "monitor"

# monitor가 호출할 수 있는 API (기본 차단). 정적 파일은 역할과 무관하게 허용한다.
_MONITOR_DENY_GET = re.compile(r"^/api/nodes/[^/]+/chaos$")


def is_allowed(role: str, method: str, path: str) -> bool:
    if role == ROLE_ADMIN:
        return True
    if role != ROLE_MONITOR:
        return False
    if not path.startswith("/api/"):
        return method in ("GET", "HEAD")
    if method in ("GET", "HEAD"):
        return not _MONITOR_DENY_GET.match(path)
    return method == "POST" and path == "/api/logout"


@dataclass(frozen=True)
class Account:
    username: str
    role: str
    password_hash: str = field(repr=False)


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
    """token → (Account, 만료 monotonic). 절대 만료. console 재기동 시 전부 사라진다."""

    def __init__(self, ttl_sec: int) -> None:
        self.ttl_sec = ttl_sec
        self._sessions: dict[str, tuple[Account, float]] = {}

    def create(self, account: Account) -> str:
        self._purge()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = (account, time.monotonic() + self.ttl_sec)
        return token

    def get(self, token: str | None) -> Account | None:
        if not token:
            return None
        entry = self._sessions.get(token)
        if entry is None:
            return None
        account, expires = entry
        if time.monotonic() >= expires:
            self._sessions.pop(token, None)
            return None
        return account

    def delete(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def _purge(self) -> None:
        now = time.monotonic()
        for t in [t for t, (_, exp) in self._sessions.items() if exp <= now]:
            del self._sessions[t]


class LoginLimiter:
    """클라이언트 IP별 연속 실패 횟수. max_failures회 연속 실패 → lockout_sec 동안 차단."""

    MAX_TRACKED = 10_000  # 다수 IP로 실패를 흘려도 메모리가 무한히 늘지 않게

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
        if until > time.monotonic():
            # 차단 중: 차단 전에 시작된 병렬 요청의 늦은 실패가 차단을 풀거나 바꾸지 않게 한다 (SPEC §8)
            return
        if until:
            count = 0  # 차단이 풀린 뒤에는 처음부터 센다
        count += 1
        if len(self._state) >= self.MAX_TRACKED and ip not in self._state:
            self._prune()
        if count >= self.max_failures:
            self._state[ip] = (0, time.monotonic() + self.lockout_sec)
            log.warning("event=login_locked client=%s lockout_sec=%d", ip, self.lockout_sec)
        else:
            self._state[ip] = (count, 0.0)

    def reset(self, ip: str) -> None:
        self._state.pop(ip, None)

    def _prune(self) -> None:
        """차단 중이 아닌 항목(누적 실패만 있는 IP, 차단이 풀린 IP)을 정리한다. 차단 중인 IP는 유지."""
        now = time.monotonic()
        for ip in [ip for ip, (_, until) in self._state.items() if until <= now]:
            del self._state[ip]
        log.warning("event=login_limiter_pruned remaining=%d", len(self._state))


class Authenticator:
    def __init__(self, accounts: list[Account], session_ttl_sec: int,
                 max_failures: int, lockout_sec: int, cookie_secure: bool) -> None:
        if not accounts:
            raise RuntimeError("no accounts configured")
        names = [a.username for a in accounts]
        if len(set(names)) != len(names):
            raise RuntimeError("duplicate usernames in accounts")
        for a in accounts:
            if not a.username:
                raise RuntimeError("empty username")
            try:
                parse_hash(a.password_hash)
            except ValueError as e:
                raise RuntimeError(f"invalid password hash for {a.username!r}: {e}") from None
        self.accounts = accounts
        self.sessions = SessionStore(session_ttl_sec)
        self.limiter = LoginLimiter(max_failures, lockout_sec)
        self.cookie_secure = cookie_secure

    def _find(self, username: str) -> Account | None:
        found = None
        for a in self.accounts:  # 전부 비교 (조기 종료로 순서가 드러나지 않게)
            if hmac.compare_digest(username.encode(), a.username.encode()):
                found = a
        return found

    def check(self, username: str, password: str) -> Account | None:
        """동기 버전 (scrypt ~50ms). async 코드에서는 authenticate()를 쓴다."""
        account = self._find(username)
        ok = verify_password(password, account.password_hash if account else _DUMMY_HASH)
        return account if account is not None and ok else None

    async def authenticate(self, username: str, password: str) -> Account | None:
        # scrypt는 CPU를 쓰므로 이벤트 루프를 막지 않게 스레드에서 계산한다.
        return await asyncio.to_thread(self.check, username, password)

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

        account = self.auth.sessions.get(conn.cookies.get(SESSION_COOKIE))
        basic = None
        if account is None:
            basic = parse_basic(conn.headers.get("authorization"))
            if basic is not None:
                wait = self.auth.limiter.locked_for(ip)
                if wait:
                    await _json(429, "too many failed attempts", {"Retry-After": str(wait)})(scope, receive, send)
                    return
                account = await self.auth.authenticate(*basic)
                if account is not None:
                    self.auth.limiter.reset(ip)
                else:
                    self.auth.limiter.fail(ip)

        if account is not None:
            if not is_allowed(account.role, scope["method"], scope["path"]):
                log.warning("event=forbidden user=%s role=%s method=%s path=%s",
                            account.username, account.role, scope["method"], scope["path"])
                await _json(403, f"권한이 없습니다 ({account.role})")(scope, receive, send)
                return
            state = scope.setdefault("state", {})
            state["user"], state["role"] = account.username, account.role
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


SECURITY_HEADERS = [
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
]


class SecurityHeadersMiddleware:
    """모든 HTTP 응답에 보안 헤더를 붙인다. 제어 화면이 다른 사이트 iframe에 삽입되는 클릭재킹 방지."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message) -> None:
            if message["type"] == "http.response.start":
                names = {name for name, _ in SECURITY_HEADERS}
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                message = {**message, "headers": headers + SECURITY_HEADERS}
            await send(message)

        await self.app(scope, receive, wrapped)


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


# ---------------------------------------------------------------- CLI

def _main(argv: list[str]) -> int:
    """python -m app.auth hash  → 비밀번호를 입력받아 ADMIN_PASSWORD_HASH / MONITOR_PASSWORD_HASH 값을 출력."""
    import getpass

    if argv[1:] != ["hash"]:
        print("usage: python -m app.auth hash", file=sys.stderr)
        return 2
    if sys.stdin.isatty():
        pw = getpass.getpass("password: ")
        if pw != getpass.getpass("again: "):
            print("passwords do not match", file=sys.stderr)
            return 1
    else:
        pw = sys.stdin.readline().rstrip("\n")  # 파이프 입력 (예: 자동화)
    if len(pw) < 8:
        print("password must be at least 8 characters", file=sys.stderr)
        return 1
    print(hash_password(pw))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
