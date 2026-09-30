"""주기 헬스체크, 노드별 수집 상태 (SPEC §4.1, §5).

- 노드마다 독립 태스크로 수집하므로 한 노드의 지연이 다른 노드의 수집 시각을 늦추지 않는다.
- 노드별 in-flight 플래그: 이전 수집이 끝나지 않았으면 이번 주기는 건너뛴다.
- 루프는 어떤 예외에도 종료되지 않으며 다음 주기는 사이클 시작 시각 기준으로 계산한다.
- 헬스체크는 재시도하지 않는다. 다음 주기가 곧 재시도다.
- 판정은 poller가 한다 (SPEC §4.2, §4.4): 수집 결과 반영 직후와 매 주기 시작 시 평가해 저장하고,
  상태가 바뀌면 node_events에 기록한다. API는 저장된 판정을 반환한다.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .agent_client import AgentClient, HealthResult
from .config import Settings
from .models import HealthPayload, LastError, NodeDetail, NodeView, Sample
from .registry import Node
from .status import evaluate

log = logging.getLogger("console.poller")

EVENTS_PRUNE_INTERVAL_SEC = 3600


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventStore(Protocol):
    async def insert_event(self, node_id: str, ts: str, from_status: str | None, to_status: str,
                           reasons: list[str]) -> None: ...

    async def prune_events(self, before_ts: str) -> int: ...


@dataclass
class NodeState:
    node_id: str
    node_name: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    last_error: LastError | None = None
    latency_ms: int | None = None
    last_health: HealthPayload | None = None
    skipped_cycles: int = 0
    samples: deque[Sample] = field(default_factory=deque)  # maxlen은 Poller가 SAMPLES_MAX로 지정
    in_flight: bool = False
    # poller가 저장하는 판정 결과
    status: str = "UNKNOWN"
    reasons: list[str] = field(default_factory=lambda: ["첫 수집 대기 중"])
    evaluated_at: datetime | None = None
    announced: bool = False  # 기동 후 첫 확정 판정(이벤트) 기록 여부

    def apply(self, result: HealthResult, attempted_at: datetime) -> None:
        self.last_attempt_at = attempted_at
        if result.ok:
            assert result.health is not None
            self.last_success_at = attempted_at
            self.consecutive_failures = 0
            self.last_error = None
            self.latency_ms = result.latency_ms
            self.last_health = result.health
            m = result.health.metrics
            self.samples.append(Sample(ts=attempted_at, cpu=m.cpu_pct, mem=m.mem_pct,
                                       disk=m.disk_pct, latency=result.latency_ms))
        else:
            assert result.error is not None
            self.consecutive_failures += 1
            self.last_error = LastError(type=result.error.type, message=result.error.message)


class Poller:
    def __init__(self, nodes: dict[str, Node], client: AgentClient, settings: Settings,
                 events: EventStore | None = None) -> None:
        self.nodes = nodes
        self.client = client
        self.settings = settings
        self.events = events
        self.started_at = utc_now()
        self.last_evaluated_at: datetime | None = None  # 주기 시작 시 전체 평가 시각 (poller 생존 신호)
        self.states: dict[str, NodeState] = {
            n.id: NodeState(node_id=n.id, node_name=n.name, samples=deque(maxlen=settings.samples_max))
            for n in nodes.values()
        }
        self._sem = asyncio.Semaphore(settings.poll_concurrency)
        # create_task 참조 보관 (CLAUDE.md §4-5)
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ loop

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        interval = self.settings.poll_interval_sec
        log.info("event=poller_start nodes=%d interval_sec=%s", len(self.nodes), interval)
        next_start = loop.time()
        next_prune = loop.time()
        while True:
            cycle_start = loop.time()
            try:
                # 시간 경과만으로 바뀌는 판정(stale)도 잡도록 매 주기 전 노드를 평가한다.
                self._evaluate_all(utc_now())
                self._dispatch_cycle()
                if self.events is not None and cycle_start >= next_prune:
                    next_prune = cycle_start + EVENTS_PRUNE_INTERVAL_SEC
                    self._spawn(self._prune_events(), name="events-prune")
            except Exception:
                log.exception("event=poller_cycle_error")
            # 사이클 시작 시각 기준으로 다음 주기를 잡아 drift를 없앤다.
            # 이벤트 루프가 한 주기 이상 멈췄다면 밀린 주기는 몰아서 돌지 않고 건너뛴다.
            next_start += interval
            if next_start < cycle_start:
                next_start = cycle_start + interval
            await asyncio.sleep(max(0.0, next_start - loop.time()))

    def _dispatch_cycle(self) -> None:
        for node in self.nodes.values():
            state = self.states[node.id]
            if state.in_flight:
                state.skipped_cycles += 1
                log.warning("event=poll_skipped node_id=%s skipped_cycles=%d", node.id, state.skipped_cycles)
                continue
            state.in_flight = True
            self._spawn(self._poll_node(node, state), name=f"poll-{node.id}")

    def _spawn(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("event=poll_task_error task=%s error=%r", task.get_name(), exc)

    async def _poll_node(self, node: Node, state: NodeState) -> None:
        try:
            async with self._sem:
                attempted_at = utc_now()
                # 응답을 기다리기 전에 기록한다. 응답 후에야 기록하면 진행 중인 수집을
                # 오래된 시도(stale)로 판정할 수 있다 (긴 read timeout, 동시성 대기).
                state.last_attempt_at = attempted_at
                result = await self.client.get_health(node)
            state.apply(result, attempted_at)
            self._evaluate(state, utc_now())
            if result.ok:
                log.info("event=poll_ok node_id=%s latency_ms=%d", node.id, result.latency_ms)
            else:
                assert result.error is not None
                log.warning("event=poll_failed node_id=%s error_type=%s consecutive_failures=%d latency_ms=%d msg=%r",
                            node.id, result.error.type, state.consecutive_failures,
                            result.latency_ms, result.error.message)
        except Exception:
            log.exception("event=poll_node_error node_id=%s", node.id)
        finally:
            state.in_flight = False

    # ------------------------------------------------------------ 판정 · 이벤트

    def _evaluate_all(self, now: datetime) -> None:
        for state in self.states.values():
            self._evaluate(state, now)
        self.last_evaluated_at = now

    def _evaluate(self, state: NodeState, now: datetime) -> dict | None:
        """판정을 저장하고, 상태가 바뀌었으면 이벤트를 기록한다. 기록할 이벤트를 돌려준다 (테스트용)."""
        status, reasons = evaluate(state, now, self.settings)
        if state.announced:
            changed, from_status = status != state.status, state.status
        else:
            # 기동 후 첫 확정 판정은 from_status = None. UNKNOWN(첫 수집 대기)은 기록하지 않는다.
            changed, from_status = status != "UNKNOWN", None
        state.status, state.reasons, state.evaluated_at = status, reasons, now
        if not changed:
            return None
        state.announced = True
        event = {"node_id": state.node_id, "ts": to_iso(now), "from_status": from_status,
                 "to_status": status, "reasons": reasons}
        log.warning("event=node_status_change node_id=%s from=%s to=%s reasons=%r",
                    state.node_id, from_status, status, "; ".join(reasons))
        if self.events is not None:
            self._spawn(self._write_event(event), name=f"event-{state.node_id}")
        return event

    async def _write_event(self, event: dict) -> None:
        try:
            await self.events.insert_event(**event)
        except Exception:
            # 기록 실패가 수집·판정을 멈추면 안 된다 (SPEC §4.4)
            log.exception("event=node_event_write_failed node_id=%s to=%s", event["node_id"], event["to_status"])

    async def _prune_events(self) -> None:
        cutoff = to_iso(utc_now() - timedelta(days=self.settings.events_retention_days))
        try:
            deleted = await self.events.prune_events(cutoff)
            if deleted:
                log.info("event=node_events_pruned deleted=%d before=%s", deleted, cutoff)
        except Exception:
            log.exception("event=node_events_prune_failed")

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # ------------------------------------------------------------ views

    def view(self, node_id: str, now: datetime | None = None) -> NodeView:
        return NodeView(**self._view_fields(self.states[node_id], now or utc_now()))

    def views(self) -> list[NodeView]:
        now = utc_now()
        return [NodeView(**self._view_fields(s, now)) for s in self.states.values()]

    def detail(self, node_id: str) -> NodeDetail:
        state = self.states[node_id]
        return NodeDetail(**self._view_fields(state, utc_now()), samples=list(state.samples))

    def _view_fields(self, s: NodeState, now: datetime) -> dict:
        status, reasons = s.status, s.reasons
        # 안전장치 (SPEC §4.2): poller가 멈춰 판정이 갱신되지 않으면 저장값을 믿지 않는다.
        stale_limit = self.settings.stale_factor * self.settings.poll_interval_sec
        age = (now - (self.last_evaluated_at or self.started_at)).total_seconds()
        if age > stale_limit:
            status, reasons = "UNREACHABLE", [f"판정 갱신 중단 {int(age)}초 (poller 정지 의심)"]
        h = s.last_health
        return {
            "node_id": s.node_id,
            "node_name": s.node_name,
            "status": status,
            "reasons": reasons,
            "metrics": h.metrics if h else None,
            "daemons": h.daemons if h else [],
            "latency_ms": s.latency_ms,
            "consecutive_failures": s.consecutive_failures,
            "skipped_cycles": s.skipped_cycles,
            "last_attempt_at": s.last_attempt_at,
            "last_success_at": s.last_success_at,
            "last_error": s.last_error,
        }
