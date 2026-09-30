"""status.evaluate 판정 규칙 표(SPEC §4.2) 각 행 검증."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.models import HealthPayload, LastError
from app.poller import NodeState
from app.status import evaluate

NOW = datetime(2026, 10, 1, 3, 0, 0, tzinfo=timezone.utc)
S = Settings(_env_file=None)


def health(cpu=30.0, mem=50.0, disk=60.0, daemons=None) -> HealthPayload:
    if daemons is None:
        daemons = {"pacs-gateway": "RUNNING", "hl7-interface": "RUNNING", "emr-sync": "RUNNING"}
    return HealthPayload.model_validate({
        "node_id": "node-x",
        "node_name": "X",
        "agent_time": "2026-10-01T03:00:00Z",
        "metrics": {"cpu_pct": cpu, "mem_pct": mem, "disk_pct": disk},
        "daemons": [
            {"name": n, "status": st, "pid": 100 if st == "RUNNING" else None, "uptime_sec": 1}
            for n, st in daemons.items()
        ],
    })


def state(*, success_age: float | None = 1.0, failures: int = 0, error: str = "TIMEOUT",
          latency: int | None = 10, h: HealthPayload | None = None) -> NodeState:
    s = NodeState(node_id="node-x", node_name="X")
    if success_age is not None:
        s.last_success_at = NOW - timedelta(seconds=success_age)
        s.last_health = h or health()
        s.latency_ms = latency
    s.consecutive_failures = failures
    if failures:
        s.last_error = LastError(type=error, message="x")
    return s


# ---------------------------------------------------------------- 1. UNKNOWN

def test_unknown_before_first_poll():
    assert evaluate(state(success_age=None), NOW, S) == ("UNKNOWN", ["첫 수집 대기 중"])


@pytest.mark.parametrize("failures", [1, 2])
def test_unknown_no_success_below_threshold(failures):
    status, reasons = evaluate(state(success_age=None, failures=failures), NOW, S)
    assert status == "UNKNOWN"
    assert reasons == [f"{failures}회 연속 TIMEOUT"]


# ---------------------------------------------------------------- 2. UNREACHABLE

def test_unreachable_no_success_at_threshold():
    status, reasons = evaluate(state(success_age=None, failures=3, error="CONNECT_ERROR"), NOW, S)
    assert status == "UNREACHABLE"
    assert reasons == ["3회 연속 CONNECT_ERROR"]


def test_unreachable_failures_override_last_metrics():
    # 마지막 성공 값이 CRITICAL이어도 통신두절이 우선
    h = health(daemons={"pacs-gateway": "RUNNING", "hl7-interface": "RUNNING", "emr-sync": "STOPPED"})
    status, reasons = evaluate(state(success_age=12, failures=3, h=h), NOW, S)
    assert status == "UNREACHABLE"
    assert reasons == ["3회 연속 TIMEOUT"]


def test_unreachable_stale_without_failures():
    # poller 정지 등으로 실패 카운트 없이 오래된 경우 (> 3 × 5s)
    status, reasons = evaluate(state(success_age=16), NOW, S)
    assert status == "UNREACHABLE"
    assert reasons == ["마지막 성공 16s 전 > 15s"]


def test_not_stale_at_boundary():
    assert evaluate(state(success_age=15), NOW, S)[0] == "HEALTHY"


# ---------------------------------------------------------------- 3. CRITICAL

def test_critical_stopped_daemon():
    h = health(daemons={"pacs-gateway": "RUNNING", "hl7-interface": "RUNNING", "emr-sync": "STOPPED"})
    assert evaluate(state(h=h), NOW, S) == ("CRITICAL", ["daemon emr-sync STOPPED"])


@pytest.mark.parametrize("kw,reason", [
    ({"cpu": 95.0}, "cpu 95.0% ≥ 95"),
    ({"mem": 97.3}, "mem 97.3% ≥ 95"),
    ({"disk": 91.2}, "disk 91.2% ≥ 90"),
])
def test_critical_metric(kw, reason):
    assert evaluate(state(h=health(**kw)), NOW, S) == ("CRITICAL", [reason])


def test_critical_beats_warning_conditions():
    # 1회 실패 + 지연이 있어도 CRITICAL 규칙이 먼저 적용된다
    status, reasons = evaluate(state(h=health(disk=92.0), failures=1, latency=2000), NOW, S)
    assert status == "CRITICAL"
    assert reasons == ["disk 92.0% ≥ 90"]


# ---------------------------------------------------------------- 4. WARNING

@pytest.mark.parametrize("kw,reason", [
    ({"cpu": 80.0}, "cpu 80.0% ≥ 80"),
    ({"mem": 85.0}, "mem 85.0% ≥ 85"),
    ({"disk": 89.0}, "disk 89.0% ≥ 80"),
])
def test_warning_metric(kw, reason):
    assert evaluate(state(h=health(**kw)), NOW, S) == ("WARNING", [reason])


@pytest.mark.parametrize("failures", [1, 2])
def test_warning_recent_failures_with_success_history(failures):
    status, reasons = evaluate(state(success_age=5 * failures, failures=failures), NOW, S)
    assert status == "WARNING"
    assert reasons == [f"수집 실패 {failures}회 (TIMEOUT)"]


def test_warning_slow_response():
    assert evaluate(state(latency=1820), NOW, S) == ("WARNING", ["응답 1820ms ≥ 1500"])


def test_warning_restarting_daemon():
    h = health(daemons={"pacs-gateway": "RESTARTING", "hl7-interface": "RUNNING", "emr-sync": "RUNNING"})
    assert evaluate(state(h=h), NOW, S) == ("WARNING", ["daemon pacs-gateway RESTARTING"])


def test_warning_collects_all_reasons():
    status, reasons = evaluate(state(h=health(cpu=81.0, disk=85.5), latency=1500), NOW, S)
    assert status == "WARNING"
    assert reasons == ["cpu 81.0% ≥ 80", "disk 85.5% ≥ 80", "응답 1500ms ≥ 1500"]


# ---------------------------------------------------------------- 5. HEALTHY

def test_healthy():
    assert evaluate(state(), NOW, S) == ("HEALTHY", [])


def test_healthy_just_below_thresholds():
    h = health(cpu=79.9, mem=84.9, disk=79.9)
    assert evaluate(state(h=h, latency=1499), NOW, S) == ("HEALTHY", [])
