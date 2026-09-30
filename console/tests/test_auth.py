"""비밀번호 해시 · 계정/역할 권한 · 세션 · 로그인 실패 제한 · Basic 헤더 파싱 (SPEC §8, §11)."""
from __future__ import annotations

import asyncio
import base64

import pytest

from app import auth as auth_mod
from app.auth import (ROLE_ADMIN, ROLE_MONITOR, Account, Authenticator, LoginLimiter, SessionStore, hash_password,
                      is_allowed, parse_basic, parse_hash, verify_password)
from app.config import DEFAULT_ADMIN_PASSWORD_HASH, DEFAULT_MONITOR_PASSWORD_HASH

# 테스트 속도를 위해 한 번만 계산해 재사용한다 (scrypt ~50ms)
ADMIN = Account("admin", ROLE_ADMIN, hash_password("admin-pw-123"))
MON = Account("monuser", ROLE_MONITOR, hash_password("mon-pw-456"))


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def patch_clock(monkeypatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(auth_mod.time, "monotonic", clock)
    return clock


def test_session_create_get_delete(monkeypatch):
    patch_clock(monkeypatch)
    s = SessionStore(ttl_sec=60)
    token = s.create(ADMIN)
    assert s.get(token) == ADMIN
    assert s.get("forged-token") is None
    assert s.get(None) is None
    s.delete(token)
    assert s.get(token) is None


def test_session_absolute_expiry(monkeypatch):
    clock = patch_clock(monkeypatch)
    s = SessionStore(ttl_sec=60)
    token = s.create(MON)
    clock.t += 59
    assert s.get(token) == MON
    clock.t += 1
    assert s.get(token) is None


def test_session_tokens_are_unique():
    s = SessionStore(ttl_sec=60)
    assert len({s.create(ADMIN) for _ in range(100)}) == 100


def test_limiter_locks_after_max_failures_and_unlocks(monkeypatch):
    clock = patch_clock(monkeypatch)
    lim = LoginLimiter(max_failures=3, lockout_sec=60)
    for _ in range(2):
        lim.fail("1.2.3.4")
    assert lim.locked_for("1.2.3.4") == 0
    lim.fail("1.2.3.4")
    assert lim.locked_for("1.2.3.4") > 0
    assert lim.locked_for("5.6.7.8") == 0  # 다른 IP는 영향 없음
    clock.t += 61
    assert lim.locked_for("1.2.3.4") == 0
    lim.fail("1.2.3.4")  # 해제 후에는 처음부터 센다
    assert lim.locked_for("1.2.3.4") == 0


def test_limiter_reset_on_success(monkeypatch):
    patch_clock(monkeypatch)
    lim = LoginLimiter(max_failures=3, lockout_sec=60)
    lim.fail("ip")
    lim.fail("ip")
    lim.reset("ip")
    lim.fail("ip")
    lim.fail("ip")
    assert lim.locked_for("ip") == 0


def make_auth() -> Authenticator:
    return Authenticator([ADMIN, MON], session_ttl_sec=60, max_failures=5, lockout_sec=60, cookie_secure=False)


def test_check_credentials_returns_account_with_role():
    a = make_auth()
    assert a.check("admin", "admin-pw-123") == ADMIN
    assert a.check("monuser", "mon-pw-456") == MON
    assert a.check("admin", "mon-pw-456") is None  # 다른 계정의 비밀번호
    assert a.check("admin", "ADMIN-PW-123") is None
    assert a.check("root", "admin-pw-123") is None  # 없는 사용자 (더미 해시로 같은 비용)
    assert a.check("", "") is None


def test_authenticate_runs_off_event_loop():
    assert asyncio.run(make_auth().authenticate("monuser", "mon-pw-456")) == MON


def test_authenticator_rejects_bad_config():
    with pytest.raises(RuntimeError, match="duplicate"):
        Authenticator([ADMIN, Account("admin", ROLE_MONITOR, MON.password_hash)], 60, 5, 60, False)
    with pytest.raises(RuntimeError, match="invalid password hash"):
        Authenticator([Account("admin", ROLE_ADMIN, "nodewatch")], 60, 5, 60, False)  # 평문을 해시 자리에


# ---------------------------------------------------------------- 해시

def test_hash_roundtrip_and_salt():
    h1, h2 = hash_password("same-password"), hash_password("same-password")
    assert h1 != h2  # salt가 달라 같은 비밀번호도 해시가 다르다
    assert verify_password("same-password", h1) and verify_password("same-password", h2)
    assert not verify_password("same-passwordX", h1)
    assert "$" not in h1  # .env·compose 변수 치환에 걸리지 않는 형식


def test_default_hashes_match_documented_passwords():
    assert verify_password("nodewatch", DEFAULT_ADMIN_PASSWORD_HASH)
    assert verify_password("monwatch", DEFAULT_MONITOR_PASSWORD_HASH)


@pytest.mark.parametrize("bad", [
    "nodewatch",                                   # 평문
    "bcrypt:10:x:y",                               # 다른 형식
    "scrypt:16384:8:1:c2FsdHNhbHQ",                # 필드 누락
    "scrypt:1073741824:8:1:c2FsdHNhbHQ:aGFzaGhhc2hoYXNoaGFzaA",  # N 과대 (서비스 거부)
    "scrypt:16385:8:1:c2FsdHNhbHQ:aGFzaGhhc2hoYXNoaGFzaA",       # N이 2의 거듭제곱 아님
])
def test_parse_hash_rejects(bad):
    with pytest.raises(ValueError):
        parse_hash(bad)


# ---------------------------------------------------------------- 역할 권한

@pytest.mark.parametrize("method,path,admin,monitor", [
    ("GET", "/", True, True),
    ("GET", "/app.js", True, True),
    ("GET", "/api/nodes", True, True),
    ("GET", "/api/nodes/node-a", True, True),
    ("GET", "/api/events", True, True),
    ("GET", "/api/jobs", True, True),
    ("GET", "/api/jobs/abc", True, True),
    ("GET", "/api/actions", True, True),
    ("GET", "/api/me", True, True),
    ("POST", "/api/logout", True, True),
    ("POST", "/api/jobs", True, False),
    ("POST", "/api/jobs/abc/reconcile", True, False),
    ("POST", "/api/jobs/abc/retry", True, False),
    ("GET", "/api/nodes/node-c/chaos", True, False),
    ("POST", "/api/nodes/node-c/chaos", True, False),
    ("DELETE", "/api/jobs/abc", True, False),       # 미래에 추가될 라우트도 기본 차단
    ("POST", "/api/anything-new", True, False),
])
def test_role_permissions(method, path, admin, monitor):
    assert is_allowed(ROLE_ADMIN, method, path) is admin
    assert is_allowed(ROLE_MONITOR, method, path) is monitor


def test_unknown_role_denied():
    assert not is_allowed("guest", "GET", "/api/nodes")


def test_parse_basic():
    good = "Basic " + base64.b64encode(b"admin:p:w").decode()
    assert parse_basic(good) == ("admin", "p:w")  # 비밀번호의 ':'는 보존
    assert parse_basic(None) is None
    assert parse_basic("Bearer x") is None
    assert parse_basic("Basic !!!") is None
    assert parse_basic("Basic " + base64.b64encode(b"nocolon").decode()) is None


def test_limiter_prunes_when_full(monkeypatch):
    clock = patch_clock(monkeypatch)
    lim = LoginLimiter(max_failures=2, lockout_sec=60)
    monkeypatch.setattr(LoginLimiter, "MAX_TRACKED", 5)
    lim.fail("locked")
    lim.fail("locked")                     # 차단 중
    for i in range(4):
        lim.fail(f"ip{i}")                 # 누적 실패만 있는 IP 4개 → 합계 5개
    lim.fail("new-ip")                     # 상한 도달 → 정리
    assert lim.locked_for("locked") > 0    # 차단 중인 IP는 유지
    assert len(lim._state) <= 2            # locked + new-ip
    clock.t += 61
    assert lim.locked_for("locked") == 0
