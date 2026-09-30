"""agent 호출 + 예외 분류 (SPEC §6).

노드 단위 호출 함수는 어떤 예외도 밖으로 던지지 않고 결과 객체를 돌려준다.
asyncio.TaskGroup은 한 태스크의 예외가 형제 태스크를 취소하고, gather는 첫 예외를 전파하기 때문이다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from .config import Settings
from .models import CommandPayload, HealthPayload
from .registry import Node

log = logging.getLogger("console.agent_client")

TOKEN_HEADER = "X-Agent-Token"
ERROR_BODY_MAX = 200


@dataclass
class CallError:
    type: str  # CONNECT_ERROR | TIMEOUT | AGENT_ERROR
    message: str
    http_status: int | None = None  # 응답을 받은 경우의 HTTP 상태 (200이면 스키마 불일치)


@dataclass
class HealthResult:
    node_id: str
    latency_ms: int
    health: HealthPayload | None = None
    error: CallError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class CommandResult:
    node_id: str
    command_id: str
    duration_ms: int
    payload: CommandPayload | None = None
    error: CallError | None = None


@dataclass
class ChaosResult:
    status_code: int | None  # agent 응답 코드. 전송 실패면 None
    body: dict | None = None
    error: CallError | None = None


def classify_exception(exc: BaseException, timeout: httpx.Timeout) -> CallError:
    """httpx 예외 → error_type. 순서가 중요하다.

    ConnectTimeout은 TimeoutException의 하위 클래스이므로 Connect 계열을 먼저 잡아야
    "미전달"이 "결과 미확인"으로 잘못 분류되지 않는다.
    """
    name = type(exc).__name__
    if isinstance(exc, httpx.ConnectTimeout):
        return CallError("CONNECT_ERROR", f"ConnectTimeout after {timeout.connect}s")
    if isinstance(exc, httpx.PoolTimeout):
        return CallError("CONNECT_ERROR", f"PoolTimeout after {timeout.pool}s")
    if isinstance(exc, httpx.ConnectError):
        return CallError("CONNECT_ERROR", f"ConnectError: {exc}")
    if isinstance(exc, httpx.ReadTimeout):
        return CallError("TIMEOUT", f"ReadTimeout after {timeout.read}s")
    if isinstance(exc, httpx.WriteTimeout):
        return CallError("TIMEOUT", f"WriteTimeout after {timeout.write}s")
    if isinstance(exc, (httpx.ReadError, httpx.RemoteProtocolError)):
        return CallError("TIMEOUT", f"{name}: {exc}")
    if isinstance(exc, httpx.TransportError):
        # TODO(question): SPEC §6 표에 없는 전송 오류(WriteError, ProxyError 등).
        #   전달 여부를 알 수 없으므로 보수적으로 TIMEOUT(결과 미확인)으로 분류함.
        return CallError("TIMEOUT", f"{name}: {exc}")
    return CallError("AGENT_ERROR", f"{name}: {exc}")


def _http_error(resp: httpx.Response) -> CallError:
    body = resp.text[:ERROR_BODY_MAX].replace("\n", " ")
    return CallError("AGENT_ERROR", f"HTTP {resp.status_code}: {body}", http_status=resp.status_code)


class AgentClient:
    def __init__(self, settings: Settings) -> None:
        self.health_timeout = httpx.Timeout(
            connect=settings.health_connect_timeout,
            read=settings.health_read_timeout,
            write=settings.health_read_timeout,
            pool=settings.health_connect_timeout,
        )
        self.cmd_timeout = httpx.Timeout(
            connect=settings.cmd_connect_timeout,
            read=settings.cmd_read_timeout,
            write=settings.cmd_read_timeout,
            pool=settings.cmd_connect_timeout,
        )
        # 헬스체크와 명령이 서로의 커넥션 슬롯을 잠식하지 않도록 두 동시성 상한의 합으로 둔다.
        limits = httpx.Limits(
            max_connections=settings.poll_concurrency + settings.job_concurrency,
            max_keepalive_connections=settings.poll_concurrency + settings.job_concurrency,
        )
        self._client = httpx.AsyncClient(limits=limits, follow_redirects=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_health(self, node: Node) -> HealthResult:
        """GET /health. 예외를 던지지 않는다. 재시도하지 않는다 (다음 주기가 재시도)."""
        started = time.monotonic()

        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            resp = await self._client.get(
                f"{node.url}/health",
                headers={TOKEN_HEADER: node.token},
                timeout=self.health_timeout,
            )
        except Exception as e:  # 분류는 classify_exception이 전담
            return HealthResult(node.id, elapsed(), error=classify_exception(e, self.health_timeout))

        latency = elapsed()
        if resp.status_code != 200:
            return HealthResult(node.id, latency, error=_http_error(resp))
        try:
            health = HealthPayload.model_validate_json(resp.content)
        except ValidationError as e:
            return HealthResult(
                node.id, latency,
                error=CallError("AGENT_ERROR", f"schema mismatch: {e.error_count()} error(s)"),
            )
        if health.node_id != node.id:
            return HealthResult(
                node.id, latency,
                error=CallError("AGENT_ERROR", f"node_id mismatch: got {health.node_id!r}"),
            )
        return HealthResult(node.id, latency, health=health)

    # ------------------------------------------------------------ commands (S3)

    async def post_command(self, node: Node, command_id: str, action: str, params: dict) -> CommandResult:
        """POST /commands. 예외를 던지지 않는다. 자동 재시도하지 않는다 (CLAUDE.md §4-8)."""
        return await self._command_call(
            node, command_id, "POST", f"{node.url}/commands",
            json={"command_id": command_id, "action": action, "params": params},
        )

    async def get_command(self, node: Node, command_id: str) -> CommandResult:
        """GET /commands/{command_id} (reconcile용). 기록 없으면 error.http_status == 404."""
        return await self._command_call(node, command_id, "GET", f"{node.url}/commands/{command_id}")

    async def _command_call(self, node: Node, command_id: str, method: str, url: str,
                            json: dict | None = None) -> CommandResult:
        started = time.monotonic()

        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            resp = await self._client.request(
                method, url, json=json, headers={TOKEN_HEADER: node.token}, timeout=self.cmd_timeout,
            )
        except Exception as e:  # 분류는 classify_exception이 전담
            return CommandResult(node.id, command_id, elapsed(), error=classify_exception(e, self.cmd_timeout))

        duration = elapsed()
        if resp.status_code != 200:
            return CommandResult(node.id, command_id, duration, error=_http_error(resp))
        try:
            payload = CommandPayload.model_validate_json(resp.content)
        except ValidationError as e:
            return CommandResult(node.id, command_id, duration, error=CallError(
                "AGENT_ERROR", f"schema mismatch: {e.error_count()} error(s)", http_status=200))
        if payload.command_id != command_id:
            return CommandResult(node.id, command_id, duration, error=CallError(
                "AGENT_ERROR", f"command_id mismatch: got {payload.command_id!r}", http_status=200))
        return CommandResult(node.id, command_id, duration, payload=payload)

    # ------------------------------------------------------------ chaos relay (데모용)

    async def chaos(self, node: Node, body: dict | None) -> ChaosResult:
        """GET(body=None) / POST /chaos 중계. 예외를 던지지 않는다."""
        try:
            if body is None:
                resp = await self._client.get(f"{node.url}/chaos", headers={TOKEN_HEADER: node.token},
                                              timeout=self.health_timeout)
            else:
                resp = await self._client.post(f"{node.url}/chaos", json=body, headers={TOKEN_HEADER: node.token},
                                               timeout=self.health_timeout)
        except Exception as e:
            return ChaosResult(None, error=classify_exception(e, self.health_timeout))
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return ChaosResult(resp.status_code, error=_http_error(resp))
        return ChaosResult(resp.status_code, body=data)
