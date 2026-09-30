"""poller 판정 저장 · 상태 전이 이벤트 · 판정 갱신 중단 안전장치 (SPEC §4.2, §4.4)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.models import HealthPayload, LastError
from app.poller import NodeState, Poller
from app.registry import Node

NOW = datetime(2026, 10, 1, 3, 0, 0, tzinfo=timezone.utc)
S = Settings(_env_file=None)


class FakeStore:
    def __init__(self, fail: bool = False) -> None:
        self.rows: list[dict] = []
        self.fail = fail

    async def insert_event(self, **event) -> None:
        if self.fail:
            raise RuntimeError("disk full")
        self.rows.append(event)

    async def prune_events(self, before_ts: str) -> int:
        return 0


def health(emr: str = "RUNNING") -> HealthPayload:
    return HealthPayload.model_validate({
        "node_id": "node-c", "node_name": "C", "agent_time": "2026-10-01T03:00:00Z",
        "metrics": {"cpu_pct": 30, "mem_pct": 50, "disk_pct": 60},
        "daemons": [{"name": "emr-sync", "status": emr, "pid": 1 if emr == "RUNNING" else None, "uptime_sec": 1}],
    })


def make_poller(store=None) -> tuple[Poller, NodeState]:
    nodes = {"node-c": Node(id="node-c", name="C", url="http://node-c:9000", token="t")}
    p = Poller(nodes, client=None, settings=S, events=store)  # client는 판정 테스트에 쓰지 않음
    return p, p.states["node-c"]


def succeed(s: NodeState, at: datetime, h: HealthPayload) -> None:
    s.last_attempt_at = s.last_success_at = at
    s.consecutive_failures, s.last_error, s.last_health, s.latency_ms = 0, None, h, 5


def fail(s: NodeState, at: datetime) -> None:
    s.last_attempt_at = at
    s.consecutive_failures += 1
    s.last_error = LastError(type="TIMEOUT", message="x")


# ---------------------------------------------------------------- 전이 판정 (동기, 이벤트 기록 없이)

def test_unknown_before_first_success_is_not_an_event():
    p, s = make_poller()
    assert p._evaluate(s, NOW) is None
    assert (s.status, s.announced) == ("UNKNOWN", False)


def test_first_definite_status_has_null_from():
    p, s = make_poller()
    succeed(s, NOW, health(emr="STOPPED"))
    ev = p._evaluate(s, NOW)
    assert ev == {"node_id": "node-c", "ts": "2026-10-01T03:00:00Z", "from_status": None,
                  "to_status": "CRITICAL", "reasons": ["daemon emr-sync STOPPED"]}


def test_same_status_records_nothing():
    p, s = make_poller()
    succeed(s, NOW, health(emr="STOPPED"))
    p._evaluate(s, NOW)
    for i in range(1, 5):
        succeed(s, NOW + timedelta(seconds=5 * i), health(emr="STOPPED"))
        assert p._evaluate(s, NOW + timedelta(seconds=5 * i)) is None


def test_blackhole_scenario_transitions():
    # CRITICAL → (실패 1·2회는 CRITICAL 유지) → 3회째 UNREACHABLE → 복구 시 CRITICAL
    p, s = make_poller()
    succeed(s, NOW, health(emr="STOPPED"))
    events = [p._evaluate(s, NOW)]
    t = NOW
    for _ in range(3):
        t += timedelta(seconds=5)
        fail(s, t)
        events.append(p._evaluate(s, t))
    t += timedelta(seconds=5)
    succeed(s, t, health(emr="STOPPED"))
    events.append(p._evaluate(s, t))
    got = [(e["from_status"], e["to_status"]) for e in events if e]
    assert got == [(None, "CRITICAL"), ("CRITICAL", "UNREACHABLE"), ("UNREACHABLE", "CRITICAL")]


def test_never_reachable_node_first_event_is_unreachable():
    p, s = make_poller()
    evs = []
    for i in range(3):
        fail(s, NOW + timedelta(seconds=5 * i))
        evs.append(p._evaluate(s, NOW + timedelta(seconds=5 * i)))
    assert [e for e in evs if e] == [{"node_id": "node-c", "ts": "2026-10-01T03:00:10Z", "from_status": None,
                                      "to_status": "UNREACHABLE", "reasons": ["3회 연속 TIMEOUT"]}]


# ---------------------------------------------------------------- 이벤트 기록 (비동기)

def test_events_are_written_to_store():
    async def run():
        store = FakeStore()
        p, s = make_poller(store)
        succeed(s, NOW, health())
        p._evaluate(s, NOW)
        await asyncio.gather(*p._tasks)
        return store.rows
    rows = asyncio.run(run())
    assert [(r["from_status"], r["to_status"]) for r in rows] == [(None, "HEALTHY")]


def test_store_failure_does_not_break_evaluation():
    async def run():
        p, s = make_poller(FakeStore(fail=True))
        succeed(s, NOW, health())
        p._evaluate(s, NOW)
        await asyncio.gather(*p._tasks, return_exceptions=True)
        return s.status
    assert asyncio.run(run()) == "HEALTHY"


# ---------------------------------------------------------------- API 안전장치

def test_view_uses_stored_status():
    p, s = make_poller()
    succeed(s, NOW, health(emr="STOPPED"))
    p._evaluate_all(NOW)
    v = p.view("node-c", now=NOW + timedelta(seconds=4))
    assert (v.status, v.reasons) == ("CRITICAL", ["daemon emr-sync STOPPED"])


def test_view_guard_when_poller_stopped():
    p, s = make_poller()
    succeed(s, NOW, health())
    p._evaluate_all(NOW)
    v = p.view("node-c", now=NOW + timedelta(seconds=16))  # > 3 × 5s
    assert v.status == "UNREACHABLE"
    assert v.reasons == ["판정 갱신 중단 16초 (poller 정지 의심)"]
    assert s.status == "HEALTHY"  # 저장된 판정·이벤트는 건드리지 않는다
