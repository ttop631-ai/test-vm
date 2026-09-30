"""HTTP Basic 인증 미들웨어 (SPEC §8, §11). /healthz만 예외.

라우터 dependency는 StaticFiles mount에 적용되지 않아 대시보드가 무인증으로 열리므로
반드시 미들웨어로 건다. 비교는 hmac.compare_digest. 비밀번호는 로그에 남기지 않는다.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("console.auth")

PUBLIC_PATHS = frozenset({"/healthz"})
REALM = 'Basic realm="NodeWatch", charset="UTF-8"'


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


class BasicAuthMiddleware:
    """순수 ASGI 미들웨어. 인증된 사용자명은 scope["state"]["user"]에 둔다 (requested_by 기록용)."""

    def __init__(self, app: ASGIApp, username: str, password: str) -> None:
        if not username or not password:
            raise RuntimeError("ADMIN_USER and ADMIN_PASSWORD must be set")
        self.app = app
        self._user = username.encode()
        self._password = password.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        creds = parse_basic(headers.get(b"authorization", b"").decode("latin-1") or None)
        if creds is not None:
            user, password = creds
            # 두 비교를 모두 수행해 사용자명 일치 여부가 응답 시간으로 드러나지 않게 한다.
            user_ok = hmac.compare_digest(user.encode(), self._user)
            pw_ok = hmac.compare_digest(password.encode(), self._password)
            if user_ok and pw_ok:
                scope.setdefault("state", {})["user"] = user
                await self.app(scope, receive, send)
                return

        client = scope.get("client")
        log.warning("event=auth_failed path=%s client=%s credentials=%s",
                    scope["path"], client[0] if client else "-", "present" if creds else "missing")
        response = JSONResponse({"detail": "authentication required"}, status_code=401,
                                headers={"WWW-Authenticate": REALM})
        await response(scope, receive, send)
