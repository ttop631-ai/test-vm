"""세션 저장소 · 로그인 실패 제한 · Basic 헤더 파싱 (SPEC §8, §11)."""
from __future__ import annotations

import base64

from app import auth as auth_mod
from app.auth import Authenticator, LoginLimiter, SessionStore, parse_basic


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
    token = s.create("admin")
    assert s.get(token) == "admin"
    assert s.get("forged-token") is None
    assert s.get(None) is None
    s.delete(token)
    assert s.get(token) is None


def test_session_absolute_expiry(monkeypatch):
    clock = patch_clock(monkeypatch)
    s = SessionStore(ttl_sec=60)
    token = s.create("admin")
    clock.t += 59
    assert s.get(token) == "admin"
    clock.t += 1
    assert s.get(token) is None


def test_session_tokens_are_unique():
    s = SessionStore(ttl_sec=60)
    assert len({s.create("admin") for _ in range(100)}) == 100


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


def test_check_credentials():
    a = Authenticator("admin", "pw", 60, 5, 60, False)
    assert a.check("admin", "pw")
    assert not a.check("admin", "PW")
    assert not a.check("root", "pw")
    assert not a.check("", "")


def test_parse_basic():
    good = "Basic " + base64.b64encode(b"admin:p:w").decode()
    assert parse_basic(good) == ("admin", "p:w")  # 비밀번호의 ':'는 보존
    assert parse_basic(None) is None
    assert parse_basic("Bearer x") is None
    assert parse_basic("Basic !!!") is None
    assert parse_basic("Basic " + base64.b64encode(b"nocolon").decode()) is None
