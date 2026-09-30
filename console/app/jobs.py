"""job 생성, 워커, reconcile, retry (SPEC §5, §6, §8).

- POST /api/jobs는 DB 기록 직후 반환하고 실행은 백그라운드 태스크가 한다.
- 같은 노드에 대한 명령은 노드별 asyncio.Lock으로 직렬화한다. 획득 순서: 노드 락 → 전역 세마포어.
- 명령은 자동 재시도하지 않는다. 응답 타임아웃은 UNKNOWN으로 남기고 reconcile로 확인한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from .actions import InvalidAction, validate
from .agent_client import AgentClient, CallError, CommandResult
from .config import Settings
from .db import RESULT_STATUSES, Database
from .models import JobDetail, JobResult, JobSummary
from .registry import Node

log = logging.getLogger("console.jobs")

# create_task 참조 보관 (CLAUDE.md §4-5)
_tasks: set[asyncio.Task] = set()


def _on_task_done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("event=job_task_error task=%s error=%r", task.get_name(), exc)


def _spawn(coro, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class JobError(ValueError):
    """요청 거부 (HTTP 400)."""


def truncate_output(output: str | None, max_bytes: int) -> tuple[str | None, bool]:
    if output is None:
        return None, False
    raw = output.encode("utf-8")
    if len(raw) <= max_bytes:
        return output, False
    # 멀티바이트 문자 경계에서 잘리면 버린다.
    return raw[:max_bytes].decode("utf-8", errors="ignore"), True


def compute_job_status(counts: dict[str, int]) -> str:
    """SPEC §8 Job 상태 규칙 (INTERRUPTED 제외)."""
    if counts.get("PENDING", 0) or counts.get("RUNNING", 0):
        return "RUNNING"
    total = sum(counts.values())
    success = counts.get("SUCCESS", 0)
    if success == total and total > 0:
        return "COMPLETED"
    if success > 0:
        return "PARTIAL"
    return "FAILED"


def result_status_for_error(err: CallError) -> str:
    """SPEC §6 표: error_type → 명령 결과 상태."""
    if err.type == "CONNECT_ERROR":
        return "FAILED"  # 요청 미전달, 재시도 안전
    if err.type == "TIMEOUT":
        return "UNKNOWN"  # 전달됐을 수 있음, 자동 재시도 금지
    if err.type == "AGENT_ERROR" and err.http_status == 200:
        return "UNKNOWN"  # 200인데 스키마 불일치: 실행됐을 수 있음
    return "FAILED"  # 4xx/5xx: agent가 거부했거나 내부 오류


class JobService:
    def __init__(self, db: Database, nodes: dict[str, Node], client: AgentClient, settings: Settings) -> None:
        self.db = db
        self.nodes = nodes
        self.client = client
        self.settings = settings
        self._node_locks: dict[str, asyncio.Lock] = {}
        self._sem = asyncio.Semaphore(settings.job_concurrency)

    def _lock(self, node_id: str) -> asyncio.Lock:
        lock = self._node_locks.get(node_id)
        if lock is None:
            lock = self._node_locks[node_id] = asyncio.Lock()
        return lock

    # ------------------------------------------------------------ create

    def resolve_targets(self, targets: str | list[str]) -> list[str]:
        if targets == "all":
            return list(self.nodes)  # 디스패치 시점의 등록 노드
        if not isinstance(targets, list) or not targets:
            raise JobError("targets must be 'all' or a non-empty list")
        unknown = [t for t in targets if t not in self.nodes]
        if unknown:
            raise JobError(f"unknown node_id: {unknown}")
        if len(set(targets)) != len(targets):
            raise JobError("duplicate node_id in targets")
        return list(targets)

    async def create(self, targets: str | list[str], action: str, params: dict, requested_by: str,
                     parent_job_id: str | None = None) -> str:
        node_ids = self.resolve_targets(targets)
        try:
            params = validate(action, params)
        except InvalidAction as e:
            raise JobError(str(e)) from None

        job_id = str(uuid.uuid4())
        pairs = [(node_id, str(uuid.uuid4())) for node_id in node_ids]
        await self.db.create_job(job_id, parent_job_id, action, params, requested_by, utc_now_iso(), pairs)
        log.info("event=job_created job_id=%s parent_job_id=%s action=%s targets=%s requested_by=%s",
                 job_id, parent_job_id, action, ",".join(node_ids), requested_by)
        _spawn(self._run_job(job_id, action, params, pairs), name=f"job-{job_id}")
        return job_id

    async def retry(self, parent_job_id: str, include_unknown: bool, requested_by: str) -> str:
        parent = await self.db.get_job_row(parent_job_id)
        if parent is None:
            raise LookupError(parent_job_id)
        if parent["status"] == "RUNNING":
            raise JobError("job is still running")
        statuses = ("FAILED", "UNKNOWN") if include_unknown else ("FAILED",)
        rows = await self.db.get_results(parent_job_id, statuses)
        targets = [r["node_id"] for r in rows if r["node_id"] in self.nodes]
        if not targets:
            raise JobError(f"no retryable targets (statuses: {', '.join(statuses)})")
        params = json.loads(parent["params_json"])
        return await self.create(targets, parent["action"], params, requested_by, parent_job_id=parent_job_id)

    # ------------------------------------------------------------ worker

    async def _run_job(self, job_id: str, action: str, params: dict, pairs: list[tuple[str, str]]) -> None:
        # _run_target는 예외를 던지지 않지만, 방어적으로 return_exceptions=True를 둔다.
        results = await asyncio.gather(
            *(self._run_target(job_id, node_id, command_id, action, params) for node_id, command_id in pairs),
            return_exceptions=True,
        )
        for (node_id, command_id), r in zip(pairs, results):
            if isinstance(r, BaseException):
                log.error("event=job_target_crash job_id=%s node_id=%s command_id=%s error=%r",
                          job_id, node_id, command_id, r)
        await self._refresh_status(job_id)

    async def _run_target(self, job_id: str, node_id: str, command_id: str, action: str, params: dict) -> None:
        node = self.nodes[node_id]
        try:
            # 락을 기다리며 세마포어 슬롯을 점유하지 않도록 노드 락 → 세마포어 순서로 획득한다.
            async with self._lock(node_id):
                async with self._sem:
                    started_at = utc_now_iso()
                    await self.db.update_result(job_id, node_id, status="RUNNING", started_at=started_at)
                    log.info("event=command_send job_id=%s node_id=%s command_id=%s action=%s",
                             job_id, node_id, command_id, action)
                    result = await self.client.post_command(node, command_id, action, params)
            await self._record(job_id, result)
        except Exception as e:
            # DB 오류 등. 전송 여부를 모르므로 UNKNOWN으로 남긴다.
            log.exception("event=job_target_error job_id=%s node_id=%s command_id=%s",
                          job_id, node_id, command_id)
            try:
                await self.db.update_result(job_id, node_id, status="UNKNOWN", error_type="AGENT_ERROR",
                                            error_message=f"console internal error: {type(e).__name__}",
                                            finished_at=utc_now_iso())
            except Exception:
                log.exception("event=job_target_record_failed job_id=%s node_id=%s", job_id, node_id)

    async def _record(self, job_id: str, r: CommandResult) -> None:
        finished_at = utc_now_iso()
        if r.error is not None:
            status = result_status_for_error(r.error)
            await self.db.update_result(
                job_id, r.node_id, status=status, error_type=r.error.type, error_message=r.error.message,
                finished_at=finished_at, duration_ms=r.duration_ms,
            )
            log.warning("event=command_result job_id=%s node_id=%s command_id=%s status=%s error_type=%s "
                        "duration_ms=%d msg=%r", job_id, r.node_id, r.command_id, status, r.error.type,
                        r.duration_ms, r.error.message)
            return

        p = r.payload
        assert p is not None
        if p.state == "RUNNING":
            # 새 command_id인데 RUNNING이면 비정상. 실행 중일 수 있으므로 reconcile 대상으로 둔다.
            status, error_type, error_message = "UNKNOWN", "AGENT_ERROR", "agent에서 실행 중"
        elif p.exit_code == 0:
            status, error_type, error_message = "SUCCESS", None, None
        else:
            status, error_type, error_message = "FAILED", "EXEC_ERROR", f"exit_code {p.exit_code}"
        output, truncated = truncate_output(p.output, self.settings.output_max_bytes)
        await self.db.update_result(
            job_id, r.node_id, status=status, error_type=error_type, error_message=error_message,
            exit_code=p.exit_code, output=output, output_truncated=int(truncated),
            finished_at=finished_at, duration_ms=r.duration_ms,
        )
        log.info("event=command_result job_id=%s node_id=%s command_id=%s status=%s exit_code=%s "
                 "duration_ms=%d output_truncated=%s", job_id, r.node_id, r.command_id, status,
                 p.exit_code, r.duration_ms, truncated)

    async def _refresh_status(self, job_id: str) -> None:
        # 집계와 쓰기를 결과 갱신과 직렬화한다. INTERRUPTED는 유지한다 (SPEC §8, 중단 이력 보존).
        status = await self.db.refresh_job_status(job_id, compute_job_status, utc_now_iso())
        if status is not None:
            log.info("event=job_status job_id=%s status=%s", job_id, status)

    # ------------------------------------------------------------ reconcile

    async def start_reconcile(self, job_id: str) -> int:
        job = await self.db.get_job_row(job_id)
        if job is None:
            raise LookupError(job_id)
        rows = await self.db.get_results(job_id, ("UNKNOWN",))
        if rows:
            _spawn(self._reconcile(job_id, rows), name=f"reconcile-{job_id}")
        log.info("event=reconcile_start job_id=%s targets=%d", job_id, len(rows))
        return len(rows)

    async def _reconcile(self, job_id: str, rows: list[dict]) -> None:
        await asyncio.gather(*(self._reconcile_one(job_id, row) for row in rows), return_exceptions=True)
        await self._refresh_status(job_id)

    async def _reconcile_one(self, job_id: str, row: dict) -> None:
        node_id, command_id = row["node_id"], row["command_id"]
        node = self.nodes.get(node_id)
        try:
            if node is None:
                await self.db.update_result(job_id, node_id, error_message="레지스트리에 없는 노드")
                return
            async with self._sem:
                r = await self.client.get_command(node, command_id)

            if r.error is not None:
                if r.error.http_status == 404:
                    msg = "agent에 기록 없음 (미수신 또는 agent 재기동)"
                else:
                    msg = f"결과 재확인 실패: {r.error.type} {r.error.message}"
                await self.db.update_result(job_id, node_id, error_message=msg)
                log.warning("event=reconcile_result job_id=%s node_id=%s command_id=%s status=UNKNOWN msg=%r",
                            job_id, node_id, command_id, msg)
                return

            p = r.payload
            assert p is not None
            if p.state == "RUNNING":
                await self.db.update_result(job_id, node_id, error_message="agent에서 실행 중")
                log.info("event=reconcile_result job_id=%s node_id=%s command_id=%s status=UNKNOWN msg=running",
                         job_id, node_id, command_id)
                return

            output, truncated = truncate_output(p.output, self.settings.output_max_bytes)
            if p.exit_code == 0:
                status, error_type, error_message = "SUCCESS", None, None
            else:
                status, error_type, error_message = "FAILED", "EXEC_ERROR", f"exit_code {p.exit_code}"
            await self.db.update_result(
                job_id, node_id, status=status, error_type=error_type, error_message=error_message,
                exit_code=p.exit_code, output=output, output_truncated=int(truncated), reconciled=1,
            )
            log.info("event=reconcile_result job_id=%s node_id=%s command_id=%s status=%s exit_code=%s",
                     job_id, node_id, command_id, status, p.exit_code)
        except Exception:
            log.exception("event=reconcile_error job_id=%s node_id=%s command_id=%s", job_id, node_id, command_id)

    # ------------------------------------------------------------ views

    @staticmethod
    def _result_view(r: dict) -> JobResult:
        return JobResult(
            node_id=r["node_id"], command_id=r["command_id"], status=r["status"],
            error_type=r["error_type"], error_message=r["error_message"], exit_code=r["exit_code"],
            output=r["output"], output_truncated=bool(r["output_truncated"]), reconciled=bool(r["reconciled"]),
            started_at=r["started_at"], finished_at=r["finished_at"], duration_ms=r["duration_ms"],
        )

    @staticmethod
    def _summary_fields(j: dict, counts: dict[str, int]) -> dict:
        return {
            "job_id": j["id"], "parent_job_id": j["parent_job_id"], "action": j["action"],
            "params": json.loads(j["params_json"]), "requested_by": j["requested_by"], "status": j["status"],
            "created_at": j["created_at"], "finished_at": j["finished_at"], "counts": counts,
        }

    async def list(self, limit: int) -> list[JobSummary]:
        return [JobSummary(**self._summary_fields(j, j["counts"])) for j in await self.db.list_jobs(limit)]

    async def detail(self, job_id: str) -> JobDetail | None:
        j = await self.db.get_job_row(job_id)
        if j is None:
            return None
        results = await self.db.get_results(job_id)
        # counts는 반환하는 results에서 계산한다 (별도 집계 쿼리는 다른 시점을 볼 수 있다)
        counts = dict.fromkeys(RESULT_STATUSES, 0)
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return JobDetail(**self._summary_fields(j, counts), results=[self._result_view(r) for r in results])

    async def stop(self) -> None:
        for t in list(_tasks):
            t.cancel()
        if _tasks:
            await asyncio.gather(*_tasks, return_exceptions=True)
