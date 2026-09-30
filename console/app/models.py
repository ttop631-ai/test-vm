"""Pydantic 스키마 (SPEC §3.3 agent 응답, §8 console API)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, PlainSerializer


def to_utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


UtcDatetime = Annotated[datetime, PlainSerializer(to_utc_z, return_type=str)]

NodeStatus = Literal["UNKNOWN", "UNREACHABLE", "CRITICAL", "WARNING", "HEALTHY"]
DaemonStatus = Literal["RUNNING", "STOPPED", "RESTARTING"]
ErrorType = Literal["CONNECT_ERROR", "TIMEOUT", "AGENT_ERROR", "EXEC_ERROR"]


# ---------------------------------------------------------------- agent 응답

class Metrics(BaseModel):
    cpu_pct: float
    mem_pct: float
    disk_pct: float


class DaemonInfo(BaseModel):
    name: str
    status: DaemonStatus
    pid: int | None
    uptime_sec: int


class HealthPayload(BaseModel):
    """agent GET /health. 불일치하면 AGENT_ERROR (SPEC §6)."""

    node_id: str
    node_name: str
    agent_time: str
    metrics: Metrics
    daemons: list[DaemonInfo]


# ---------------------------------------------------------------- console API

class LastError(BaseModel):
    type: ErrorType
    message: str


class NodeView(BaseModel):
    node_id: str
    node_name: str
    status: NodeStatus
    reasons: list[str]
    metrics: Metrics | None
    daemons: list[DaemonInfo]
    latency_ms: int | None
    consecutive_failures: int
    skipped_cycles: int
    last_attempt_at: UtcDatetime | None
    last_success_at: UtcDatetime | None
    last_error: LastError | None


class Sample(BaseModel):
    ts: UtcDatetime
    cpu: float
    mem: float
    disk: float
    latency: int


class NodeDetail(NodeView):
    """GET /api/nodes/{node_id}: NodeView + 최근 샘플 60건."""

    samples: list[Sample]
