"""Pydantic 스키마 (SPEC §3.3 agent 응답, §8 console API)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator


def to_utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


UtcDatetime = Annotated[datetime, PlainSerializer(to_utc_z, return_type=str)]

NodeStatus = Literal["UNKNOWN", "UNREACHABLE", "CRITICAL", "WARNING", "HEALTHY"]
DaemonStatus = Literal["RUNNING", "STOPPED", "RESTARTING"]
ErrorType = Literal["CONNECT_ERROR", "TIMEOUT", "AGENT_ERROR", "EXEC_ERROR"]


# ---------------------------------------------------------------- agent 응답

# 0~100의 유한값만 허용한다. NaN은 모든 임계치 비교가 거짓이라 '정상'으로 판정되고,
# API에서는 null로 직렬화되어 대시보드가 깨진다 (SPEC §6: 스키마 불일치 → AGENT_ERROR).
Percent = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]


class Metrics(BaseModel):
    cpu_pct: Percent
    mem_pct: Percent
    disk_pct: Percent


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


class CommandPayload(BaseModel):
    """agent POST /commands, GET /commands/{id}. 불일치하면 AGENT_ERROR → UNKNOWN (SPEC §6)."""

    command_id: str
    state: Literal["DONE", "RUNNING"]
    exit_code: int | None = None
    output: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    @model_validator(mode="after")
    def _done_has_exit_code(self) -> CommandPayload:
        if self.state == "DONE" and self.exit_code is None:
            raise ValueError("DONE without exit_code")
        return self


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


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(max_length=128)
    password: str = Field(max_length=256)


class NodeEvent(BaseModel):
    """상태 전이 이벤트 (SPEC §4.4). from_status가 null이면 console 기동 후 첫 판정."""

    id: int
    node_id: str
    node_name: str
    ts: str
    from_status: NodeStatus | None
    to_status: NodeStatus
    reasons: list[str]


class Sample(BaseModel):
    ts: UtcDatetime
    cpu: float
    mem: float
    disk: float
    latency: int


class NodeDetail(NodeView):
    """GET /api/nodes/{node_id}: NodeView + 최근 샘플 60건."""

    samples: list[Sample]


# ---------------------------------------------------------------- jobs (S3)

JobStatus = Literal["RUNNING", "COMPLETED", "PARTIAL", "FAILED", "INTERRUPTED"]
ResultStatus = Literal["PENDING", "RUNNING", "SUCCESS", "FAILED", "UNKNOWN"]


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: Literal["all"] | list[str]
    action: str
    params: dict = Field(default_factory=dict)


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_unknown: bool = False


class JobCreated(BaseModel):
    job_id: str


class JobResult(BaseModel):
    node_id: str
    command_id: str
    status: ResultStatus
    error_type: str | None
    error_message: str | None
    exit_code: int | None
    output: str | None
    output_truncated: bool
    reconciled: bool
    started_at: str | None
    finished_at: str | None
    duration_ms: int | None


class JobSummary(BaseModel):
    job_id: str
    parent_job_id: str | None
    action: str
    params: dict
    requested_by: str
    status: JobStatus
    created_at: str
    finished_at: str | None
    counts: dict[str, int]


class JobDetail(JobSummary):
    results: list[JobResult]
