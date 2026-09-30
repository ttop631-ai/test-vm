"""메트릭 random walk와 데몬 상태 시뮬레이션 (SPEC §3.2).

상태는 프로세스 메모리에만 있으며 재기동 시 프로필 초기값으로 돌아간다.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger("agent.simulator")

DAEMON_NAMES = ("pacs-gateway", "hl7-interface", "emr-sync")

RUNNING = "RUNNING"
STOPPED = "STOPPED"
RESTARTING = "RESTARTING"

WALK_STEP = 3.0  # tick당 ±3%p
DISK_PRESSURE_DRIFT = 0.05  # disk_pressure 프로필: tick당 누적 증가량

# PROFILE → 메트릭별 기준 범위 (lo, hi)
PROFILES: dict[str, dict[str, tuple[float, float]]] = {
    "normal": {"cpu_pct": (20, 50), "mem_pct": (40, 60), "disk_pct": (50, 65)},
    "disk_pressure": {"cpu_pct": (20, 50), "mem_pct": (40, 60), "disk_pct": (82, 88)},
    "daemon_down": {"cpu_pct": (20, 50), "mem_pct": (40, 60), "disk_pct": (50, 65)},
}
INITIAL_STOPPED: dict[str, tuple[str, ...]] = {
    "normal": (),
    "disk_pressure": (),
    "daemon_down": ("emr-sync",),
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


@dataclass
class Metric:
    value: float
    lo: float
    hi: float

    def step(self) -> None:
        # 기준 범위 안에서는 ±3%p random walk.
        # 범위 밖(액션 효과 등)이면 같은 폭 안에서 범위 쪽으로만 움직여 서서히 복귀한다.
        # TODO(question): SPEC §3.2 "프로필 기준 범위에서 random walk"의 범위 이탈 처리 방식이
        #   명시되지 않아 "범위 쪽으로 복귀"로 해석함.
        if self.value < self.lo:
            delta = random.uniform(0, WALK_STEP)
        elif self.value > self.hi:
            delta = -random.uniform(0, WALK_STEP)
        else:
            delta = random.uniform(-WALK_STEP, WALK_STEP)
        self.value = _clamp(self.value + delta)

    def shift_band(self, delta: float) -> None:
        self.lo = _clamp(self.lo + delta)
        self.hi = _clamp(self.hi + delta)


@dataclass
class Daemon:
    name: str
    status: str
    pid: int | None
    started_mono: float | None  # RUNNING 시작 시각 (time.monotonic 기준)

    def uptime_sec(self) -> int:
        if self.status != RUNNING or self.started_mono is None:
            return 0
        return int(time.monotonic() - self.started_mono)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "pid": self.pid,
            "uptime_sec": self.uptime_sec(),
        }


def new_pid() -> int:
    return random.randint(1000, 32000)


@dataclass
class Simulator:
    node_id: str
    node_name: str
    profile: str
    metrics: dict[str, Metric] = field(default_factory=dict)
    daemons: dict[str, Daemon] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(f"unknown PROFILE: {self.profile!r} (allowed: {', '.join(PROFILES)})")
        for name, (lo, hi) in PROFILES[self.profile].items():
            self.metrics[name] = Metric(value=random.uniform(lo, hi), lo=lo, hi=hi)
        now = time.monotonic()
        for name in DAEMON_NAMES:
            if name in INITIAL_STOPPED[self.profile]:
                self.daemons[name] = Daemon(name, STOPPED, None, None)
            else:
                # 기동 직후에도 uptime이 0이 아니도록 과거 1~30일 전에 시작한 것으로 둔다.
                started = now - random.randint(86400, 30 * 86400)
                self.daemons[name] = Daemon(name, RUNNING, new_pid(), started)

    def tick(self) -> None:
        if self.profile == "disk_pressure":
            # TODO(question): TICK_SEC=2 기준 약 80초 뒤 기준 범위 상단이 90(CRITICAL)에 도달한다.
            #   시연 시나리오(병원 B = 경고)와 충돌할 수 있어 상한 여부 확인 필요.
            self.metrics["disk_pct"].shift_band(DISK_PRESSURE_DRIFT)
        for m in self.metrics.values():
            m.step()

    def health(self) -> dict:
        return {
            "node_id": self.node_id,
            "node_name": self.node_name,
            "agent_time": utc_now_iso(),
            "metrics": {k: round(m.value, 1) for k, m in self.metrics.items()},
            "daemons": [d.to_dict() for d in self.daemons.values()],
        }

    def stop_daemon(self, name: str) -> None:
        d = self.daemons[name]
        d.status, d.pid, d.started_mono = STOPPED, None, None
        log.warning("event=daemon_stopped node_id=%s daemon=%s reason=chaos", self.node_id, name)

    async def run(self, tick_sec: float) -> None:
        """tick 루프. 어떤 예외에도 종료되지 않는다 (취소 제외)."""
        while True:
            started = time.monotonic()
            try:
                self.tick()
            except Exception:
                log.exception("event=tick_error node_id=%s", self.node_id)
            await asyncio.sleep(max(0.0, tick_sec - (time.monotonic() - started)))
