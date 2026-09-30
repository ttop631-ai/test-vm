"""주기 헬스체크, 노드별 수집 상태 (SPEC §4.1, §5).

- 노드마다 독립 태스크로 수집하므로 한 노드의 지연이 다른 노드의 수집 시각을 늦추지 않는다.
- 노드별 in-flight 플래그: 이전 수집이 끝나지 않았으면 이번 주기는 건너뛴다.
- 루프는 어떤 예외에도 종료되지 않으며 다음 주기는 사이클 시작 시각 기준으로 계산한다.
- 헬스체크는 재시도하지 않는다. 다음 주기가 곧 재시도다.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .agent_client import AgentClient, HealthResult
from .config import Settings
from .models import HealthPayload, LastError, NodeDetail, NodeView, Sample
from .registry import Node
from .status import evaluate

log = logging.getLogger("console.poller")

SAMPLES_MAX = 60


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
    samples: deque[Sample] = field(default_factory=lambda: deque(maxlen=SAMPLES_MAX))
    in_flight: bool = False

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
    def __init__(self, nodes: dict[str, Node], client: AgentClient, settings: Settings) -> None:
        self.nodes = nodes
        self.client = client
        self.settings = settings
        self.states: dict[str, NodeState] = {
            n.id: NodeState(node_id=n.id, node_name=n.name) for n in nodes.values()
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
        while True:
            cycle_start = loop.time()
            try:
                self._dispatch_cycle()
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
            task = asyncio.create_task(self._poll_node(node, state), name=f"poll-{node.id}")
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
                result = await self.client.get_health(node)
            state.apply(result, attempted_at)
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
        status, reasons = evaluate(s, now, self.settings)
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
