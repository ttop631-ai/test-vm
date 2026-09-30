"""장애 주입: 지연·오류·blackhole·데몬 중지 (SPEC §3.3 POST /chaos)."""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .simulator import DAEMON_NAMES, Simulator

log = logging.getLogger("agent.chaos")

DaemonName = Literal["pacs-gateway", "hl7-interface", "emr-sync"]
assert set(DaemonName.__args__) == set(DAEMON_NAMES)


class ChaosUpdate(BaseModel):
    """부분 갱신. 지정한 필드만 바뀐다. reset=true면 전체 초기화 (데몬 상태는 유지)."""

    model_config = ConfigDict(extra="forbid")

    latency_ms: int | None = Field(default=None, ge=0, le=60_000)
    error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    blackhole: bool | None = None
    stop_daemon: DaemonName | None = None
    reset: bool = False


class Chaos:
    def __init__(self, node_id: str, blackhole_max_hold_sec: float) -> None:
        self.node_id = node_id
        self.blackhole_max_hold_sec = blackhole_max_hold_sec
        self.latency_ms = 0
        self.error_rate = 0.0
        # set = 통과, clear = blackhole 중. 해제 시 보류 중인 응답을 풀어준다.
        self._open = asyncio.Event()
        self._open.set()

    @property
    def blackhole(self) -> bool:
        return not self._open.is_set()

    def state(self) -> dict:
        # stop_daemon은 적용 즉시 소멸하는 일회성 동작이라 상태로 보관하지 않는다.
        return {
            "latency_ms": self.latency_ms,
            "error_rate": self.error_rate,
            "blackhole": self.blackhole,
            "stop_daemon": None,
        }

    def apply(self, upd: ChaosUpdate, sim: Simulator) -> dict:
        if upd.reset:
            self.latency_ms = 0
            self.error_rate = 0.0
            self._open.set()
        if upd.latency_ms is not None:
            self.latency_ms = upd.latency_ms
        if upd.error_rate is not None:
            self.error_rate = upd.error_rate
        if upd.blackhole is not None:
            if upd.blackhole:
                self._open.clear()
            else:
                self._open.set()
        if upd.stop_daemon is not None:
            sim.stop_daemon(upd.stop_daemon)
        st = self.state()
        log.warning(
            "event=chaos_update node_id=%s reset=%s latency_ms=%d error_rate=%.2f blackhole=%s stop_daemon=%s",
            self.node_id, upd.reset, st["latency_ms"], st["error_rate"], st["blackhole"], upd.stop_daemon,
        )
        return st

    def maybe_fail(self, endpoint: str) -> None:
        """error_rate 확률로 500. /commands에서는 실행 전에 호출한다."""
        if self.error_rate > 0 and random.random() < self.error_rate:
            log.warning("event=chaos_error node_id=%s endpoint=%s", self.node_id, endpoint)
            raise HTTPException(status_code=500, detail="chaos: injected error")

    async def before_response(self, endpoint: str) -> None:
        """응답 직전 지연과 blackhole 보류."""
        if self.latency_ms > 0:
            await asyncio.sleep(self.latency_ms / 1000)
        if self.blackhole:
            log.warning("event=blackhole_hold node_id=%s endpoint=%s", self.node_id, endpoint)
            # 클라이언트가 먼저 끊어도 이 코루틴은 남으므로 최대 보류 시간을 둔다.
            try:
                await asyncio.wait_for(self._open.wait(), timeout=self.blackhole_max_hold_sec)
            except TimeoutError:
                log.warning("event=blackhole_hold_expired node_id=%s endpoint=%s", self.node_id, endpoint)
                raise HTTPException(status_code=504, detail="chaos: blackhole hold expired") from None
            log.info("event=blackhole_released node_id=%s endpoint=%s", self.node_id, endpoint)
