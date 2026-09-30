"""화이트리스트 액션 핸들러와 command_id 결과 캐시 (SPEC §3.3, §7).

문자열 명령을 실행하는 경로는 없다. 액션은 아래 핸들러 함수로만 표현되며
파라미터는 enum 값만 허용한다 (console 1차 검증 + agent 2차 검증).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from .simulator import DAEMON_NAMES, RESTARTING, RUNNING, STOPPED, Simulator, new_pid, utc_now_iso

log = logging.getLogger("agent.actions")

CACHE_MAX = 500

# action → {param: (허용값, 기본값 또는 None=필수)}
PARAM_SPEC: dict[str, dict[str, tuple[tuple, object]]] = {
    "CLEAN_LOGS": {"older_than_days": ((1, 3, 7), 7)},
    "FLUSH_CACHE": {},
    "RESTART_DAEMON": {"daemon": (DAEMON_NAMES, None)},
    "COLLECT_DIAG": {},
}


class InvalidCommand(ValueError):
    pass


def validate(action: str, params: dict) -> dict:
    """허용되지 않은 action/params면 InvalidCommand. 기본값을 채운 params를 돌려준다."""
    if action not in PARAM_SPEC:
        raise InvalidCommand(f"unknown action: {action!r}")
    spec = PARAM_SPEC[action]
    unknown = set(params) - set(spec)
    if unknown:
        raise InvalidCommand(f"unknown params for {action}: {sorted(unknown)}")
    out: dict = {}
    for name, (allowed, default) in spec.items():
        if name not in params:
            if default is None:
                raise InvalidCommand(f"missing param for {action}: {name}")
            out[name] = default
            continue
        value = params[name]
        # bool은 int의 하위 타입이므로 True == 1 로 통과하지 않게 막는다.
        if isinstance(value, bool) or value not in allowed or type(value) is not type(allowed[0]):
            raise InvalidCommand(f"invalid value for {action}.{name}: {value!r} (allowed: {list(allowed)})")
        out[name] = value
    return out


# ---------------------------------------------------------------- handlers
# 각 핸들러는 (exit_code, output)을 돌려준다.

async def _clean_logs(sim: Simulator, params: dict) -> tuple[int, str]:
    days = params["older_than_days"]
    await asyncio.sleep(random.uniform(1, 3))
    freed = random.uniform(5, 15)
    disk = sim.metrics["disk_pct"]
    before = disk.value
    disk.value = max(0.0, disk.value - freed)
    # 지운 로그는 다시 생기지 않으므로 기준 범위도 함께 내린다.
    disk.shift_band(-freed)
    today = datetime.now(timezone.utc).date()
    lines = [f"cleaning logs older than {days} days"]
    for i in range(random.randint(3, 8)):
        d = today - timedelta(days=days + i + random.randint(0, 3))
        svc = random.choice(DAEMON_NAMES)
        lines.append(f"removed /var/log/{svc}/{svc}-{d.isoformat()}.log ({random.randint(50, 900)} MB)")
    lines.append(f"disk {before:.1f}% -> {disk.value:.1f}%")
    return 0, "\n".join(lines)


async def _flush_cache(sim: Simulator, params: dict) -> tuple[int, str]:
    await asyncio.sleep(random.uniform(1, 2))
    mem = sim.metrics["mem_pct"]
    before = mem.value
    mem.value = max(0.0, mem.value - random.uniform(10, 25))
    return 0, f"flushing page cache ... ok\nmem {before:.1f}% -> {mem.value:.1f}%"


async def _restart_daemon(sim: Simulator, params: dict) -> tuple[int, str]:
    d = sim.daemons[params["daemon"]]
    if d.status == RESTARTING:
        return 1, f"{d.name} is already restarting"
    was = d.status
    d.status, d.pid, d.started_mono = RESTARTING, None, None
    try:
        await asyncio.sleep(random.uniform(3, 5))
    except asyncio.CancelledError:
        # 재시작 도중 취소: RESTARTING에 고정되면 이후 재시작이 모두 거부되므로 STOPPED로 둔다 (SPEC §3.3)
        d.status = STOPPED
        raise
    d.status, d.pid = RUNNING, new_pid()
    d.started_mono = time.monotonic()
    stop_line = f"stopping {d.name} ... ok" if was != STOPPED else f"stopping {d.name} ... not running"
    return 0, f"{stop_line}\nstarting {d.name} ... ok (pid {d.pid})"


async def _collect_diag(sim: Simulator, params: dict) -> tuple[int, str]:
    await asyncio.sleep(random.uniform(0.1, 0.8))
    h = sim.health()
    lines = [
        f"node_id: {h['node_id']}",
        f"node_name: {h['node_name']}",
        f"profile: {sim.profile}",
        f"agent_time: {h['agent_time']}",
        "metrics:",
        *(f"  {k}: {v}" for k, v in h["metrics"].items()),
        "daemons:",
        *(f"  {d['name']}: {d['status']} pid={d['pid']} uptime_sec={d['uptime_sec']}" for d in h["daemons"]),
    ]
    return 0, "\n".join(lines)


HANDLERS: dict[str, Callable[[Simulator, dict], Awaitable[tuple[int, str]]]] = {
    "CLEAN_LOGS": _clean_logs,
    "FLUSH_CACHE": _flush_cache,
    "RESTART_DAEMON": _restart_daemon,
    "COLLECT_DIAG": _collect_diag,
}
assert HANDLERS.keys() == PARAM_SPEC.keys()


# ---------------------------------------------------------------- 결과 캐시 / 실행

class CommandStore:
    """command_id → 결과. 같은 command_id는 한 번만 실행한다."""

    def __init__(self, sim: Simulator) -> None:
        self.sim = sim
        self._results: OrderedDict[str, dict] = OrderedDict()
        self._running: dict[str, asyncio.Task] = {}
        # create_task 참조 보관 (GC로 인한 태스크 소실 방지)
        self._tasks: set[asyncio.Task] = set()

    def get(self, command_id: str) -> dict | None:
        if command_id in self._running:
            return {"command_id": command_id, "state": "RUNNING"}
        return self._results.get(command_id)

    def submit(self, command_id: str, action: str, params: dict) -> tuple[asyncio.Task | None, dict | None]:
        """새 명령이면 실행 태스크를, 이미 본 command_id면 (None, 기존 결과)를 돌려준다.

        await 없이 확인·등록하므로 단일 이벤트 루프에서 원자적이다.
        """
        existing = self.get(command_id)
        if existing is not None:
            log.info("event=command_duplicate node_id=%s command_id=%s state=%s",
                     self.sim.node_id, command_id, existing["state"])
            return None, existing
        task = asyncio.create_task(self._execute(command_id, action, params), name=f"cmd-{command_id}")
        self._running[command_id] = task
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task, None

    def _on_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            log.error("event=command_task_cancelled node_id=%s task=%s", self.sim.node_id, task.get_name())
        elif task.exception() is not None:
            log.error("event=command_task_error node_id=%s task=%s error=%r",
                      self.sim.node_id, task.get_name(), task.exception())

    async def _execute(self, command_id: str, action: str, params: dict) -> dict:
        started_at = utc_now_iso()
        log.info("event=command_start node_id=%s command_id=%s action=%s params=%s",
                 self.sim.node_id, command_id, action, params)
        try:
            exit_code, output = await HANDLERS[action](self.sim, params)
        except asyncio.CancelledError:
            # 취소돼도 결과를 남겨 같은 command_id가 영구 RUNNING이 되거나 다시 실행되지 않게 한다 (SPEC §3.3)
            self._store(command_id, {
                "command_id": command_id, "state": "DONE", "exit_code": 1, "output": "cancelled",
                "started_at": started_at, "finished_at": utc_now_iso(),
            })
            log.warning("event=command_cancelled node_id=%s command_id=%s action=%s",
                        self.sim.node_id, command_id, action)
            raise
        except Exception as e:
            log.exception("event=command_error node_id=%s command_id=%s action=%s",
                          self.sim.node_id, command_id, action)
            exit_code, output = 1, f"internal error: {type(e).__name__}"
        result = {
            "command_id": command_id,
            "state": "DONE",
            "exit_code": exit_code,
            "output": output,
            "started_at": started_at,
            "finished_at": utc_now_iso(),
        }
        self._store(command_id, result)
        log.info("event=command_done node_id=%s command_id=%s action=%s exit_code=%d",
                 self.sim.node_id, command_id, action, exit_code)
        return result

    def _store(self, command_id: str, result: dict) -> None:
        self._running.pop(command_id, None)
        self._results[command_id] = result
        while len(self._results) > CACHE_MAX:
            self._results.popitem(last=False)
