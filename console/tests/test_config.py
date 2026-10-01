"""설정값 · agent 메트릭 검증 (SPEC §5, §6). 잘못된 값은 기동·수신 단계에서 거부한다."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import HealthPayload


@pytest.mark.parametrize("field,value", [
    ("poll_interval_sec", 0),          # busy loop
    ("poll_interval_sec", -1),
    ("poll_concurrency", 0),           # 수집이 영원히 대기
    ("job_concurrency", 0),            # job이 영원히 RUNNING
    ("health_connect_timeout", 0),     # 모든 요청 즉시 실패
    ("health_read_timeout", 0),
    ("cmd_connect_timeout", 0),
    ("cmd_read_timeout", -5),
    ("http_keepalive_expiry", 0),
    ("fail_threshold", 0),             # 모든 노드가 항상 통신두절
    ("stale_factor", 0),
    ("slow_ms", 0),
    ("output_max_bytes", 0),           # 모든 출력이 빈 문자열
    ("cpu_warn_pct", 101),
    ("disk_crit_pct", -1),
    ("poll_interval_sec", float("nan")),
    ("cmd_read_timeout", float("inf")),
])
def test_invalid_settings_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize("warn,crit", [(95, 95), (99, 95)])
def test_threshold_order_enforced(warn, crit):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, cpu_warn_pct=warn, cpu_crit_pct=crit)


def test_defaults_valid():
    s = Settings(_env_file=None)
    assert s.poll_interval_sec == 5.0 and s.job_concurrency == 10


BASE = '{"node_id":"n","node_name":"n","agent_time":"t","daemons":[],"metrics":{"cpu_pct":%s,"mem_pct":1,"disk_pct":1}}'


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "150", "-5", "100.01"])
def test_invalid_metric_rejected(value):
    # 통과하면 판정이 '정상'으로 나오고 API에서는 null로 직렬화되어 대시보드가 깨진다
    with pytest.raises(ValidationError):
        HealthPayload.model_validate_json(BASE % value)


@pytest.mark.parametrize("value", ["0", "100", "42.5"])
def test_valid_metric_accepted(value):
    assert HealthPayload.model_validate_json(BASE % value).metrics.cpu_pct == float(value)


# ---------------------------------------------------------------- compose 보간 (빈 env = 기본값)

def test_empty_env_values_mean_default(monkeypatch):
    """compose는 .env에 없는 변수를 ${VAR:-}로 빈 문자열을 넘긴다. 빈 값은 기본값이어야 한다 (SPEC §2.2)."""
    for name in ("SLOW_MS", "POLL_INTERVAL_SEC", "ADMIN_USER", "ADMIN_PASSWORD_HASH", "COOKIE_SECURE",
                 "SAMPLES_MAX", "CPU_WARN_PCT", "SESSION_TTL_SEC"):
        monkeypatch.setenv(name, "")
    s = Settings(_env_file=None)
    assert (s.slow_ms, s.poll_interval_sec, s.admin_user, s.cookie_secure, s.samples_max) == (1500, 5.0, "admin", False, 60)
    assert s.admin_password_hash.startswith("scrypt:") and s.cpu_warn_pct == 80 and s.session_ttl_sec == 28800


def test_env_values_applied(monkeypatch):
    monkeypatch.setenv("SAMPLES_MAX", "720")
    monkeypatch.setenv("COOKIE_SECURE", "true")
    s = Settings(_env_file=None)
    assert s.samples_max == 720 and s.cookie_secure is True


def test_monitor_user_unset_defaults_empty_disables(monkeypatch):
    monkeypatch.delenv("MONITOR_USER", raising=False)
    assert Settings(_env_file=None).monitor_user == "monuser"
    monkeypatch.setenv("MONITOR_USER", "")
    assert Settings(_env_file=None).monitor_user == ""   # 비활성
