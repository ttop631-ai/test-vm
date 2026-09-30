"""상태 판정 순수 함수 (SPEC §4.2). I/O 없음, 단위 테스트 대상.

규칙은 위에서부터 첫 번째로 맞는 것을 적용하고, 그 규칙의 사유(reasons)를 돌려준다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .config import Settings
from .models import HealthPayload, LastError


class StateLike(Protocol):
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    last_error: LastError | None
    latency_ms: int | None
    last_health: HealthPayload | None


METRIC_LABEL = {"cpu_pct": "cpu", "mem_pct": "mem", "disk_pct": "disk"}


def _fmt(v: float) -> str:
    return f"{v:g}"


def _failure_reason(state: StateLike) -> str:
    err = state.last_error.type if state.last_error else "ERROR"
    return f"{state.consecutive_failures}회 연속 {err}"


def evaluate(state: StateLike, now: datetime, settings: Settings) -> tuple[str, list[str]]:
    failures = state.consecutive_failures
    threshold = settings.fail_threshold

    # 1. UNKNOWN: 성공 이력 없음 AND 연속 실패 < 임계
    if state.last_success_at is None and failures < threshold:
        if failures == 0:
            return "UNKNOWN", ["첫 수집 대기 중"]
        return "UNKNOWN", [_failure_reason(state)]

    # 2. UNREACHABLE: 연속 실패 ≥ 임계 OR 마지막 수집 시도가 너무 오래됨 (poller 정지 감지)
    #    last_success_at이 아니라 last_attempt_at 기준이다. 노드 장애는 실패 카운트로만 판정한다.
    reasons: list[str] = []
    if failures >= threshold:
        reasons.append(_failure_reason(state))
    stale_limit = settings.stale_factor * settings.poll_interval_sec
    if state.last_attempt_at is not None:
        age = (now - state.last_attempt_at).total_seconds()
        if age > stale_limit:
            reasons.append(f"마지막 수집 시도 {int(age)}s 전 > {_fmt(stale_limit)}s")
    if reasons:
        return "UNREACHABLE", reasons

    # 여기부터는 성공 이력이 있다 (규칙 1·2에 걸리지 않았으므로).
    health = state.last_health
    assert health is not None, "last_success_at is set but last_health is missing"
    metrics = health.metrics.model_dump()
    thresholds = settings.thresholds()

    # 3. CRITICAL: STOPPED 데몬 OR 메트릭 CRITICAL 임계 이상
    for d in health.daemons:
        if d.status == "STOPPED":
            reasons.append(f"daemon {d.name} STOPPED")
    for key, (_, crit) in thresholds.items():
        if metrics[key] >= crit:
            reasons.append(f"{METRIC_LABEL[key]} {metrics[key]:.1f}% ≥ {_fmt(crit)}")
    if reasons:
        return "CRITICAL", reasons

    # 4. WARNING: 메트릭 WARNING 임계 이상 OR 1~2회 실패 OR 지연 OR RESTARTING 데몬
    for key, (warn, _) in thresholds.items():
        if metrics[key] >= warn:
            reasons.append(f"{METRIC_LABEL[key]} {metrics[key]:.1f}% ≥ {_fmt(warn)}")
    if 0 < failures < threshold:
        reasons.append(f"수집 실패 {failures}회 ({state.last_error.type if state.last_error else 'ERROR'})")
    if state.latency_ms is not None and state.latency_ms >= settings.slow_ms:
        reasons.append(f"응답 {state.latency_ms}ms ≥ {settings.slow_ms}")
    for d in health.daemons:
        if d.status == "RESTARTING":
            reasons.append(f"daemon {d.name} RESTARTING")
    if reasons:
        return "WARNING", reasons

    # 5. HEALTHY
    return "HEALTHY", []
