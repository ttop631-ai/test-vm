"""SQLite 스키마, 쿼리 (SPEC §9). aiosqlite 단일 커넥션."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger("console.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id             TEXT PRIMARY KEY,
  parent_job_id  TEXT REFERENCES jobs(id),
  action         TEXT NOT NULL,
  params_json    TEXT NOT NULL,
  requested_by   TEXT NOT NULL,
  status         TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  finished_at    TEXT
);

CREATE TABLE IF NOT EXISTS job_results (
  job_id           TEXT NOT NULL REFERENCES jobs(id),
  node_id          TEXT NOT NULL,
  command_id       TEXT NOT NULL UNIQUE,
  status           TEXT NOT NULL,
  error_type       TEXT,
  error_message    TEXT,
  exit_code        INTEGER,
  output           TEXT,
  output_truncated INTEGER NOT NULL DEFAULT 0,
  reconciled       INTEGER NOT NULL DEFAULT 0,
  started_at       TEXT,
  finished_at      TEXT,
  duration_ms      INTEGER,
  PRIMARY KEY (job_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS node_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id       TEXT NOT NULL,
  ts            TEXT NOT NULL,
  from_status   TEXT,
  to_status     TEXT NOT NULL,
  reasons_json  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_node_events_node ON node_events(node_id, id DESC);
"""

RESULT_STATUSES = ("PENDING", "RUNNING", "SUCCESS", "FAILED", "UNKNOWN")

# job_results 중 갱신 가능한 컬럼 (update_result의 키 화이트리스트)
_RESULT_COLUMNS = {
    "status", "error_type", "error_message", "exit_code", "output", "output_truncated",
    "reconciled", "started_at", "finished_at", "duration_ms",
}


class Database:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn
        # 단일 커넥션을 여러 코루틴이 공유하므로 쓰기 트랜잭션끼리 섞이지 않게 직렬화한다.
        self._write_lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> Database:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(SCHEMA)
        await conn.commit()
        log.info("event=db_open path=%s", path)
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    # ------------------------------------------------------------ writes

    async def create_job(self, job_id: str, parent_job_id: str | None, action: str, params: dict,
                         requested_by: str, created_at: str, targets: list[tuple[str, str]]) -> None:
        """job과 대상별 PENDING 행을 한 트랜잭션으로 기록. targets = [(node_id, command_id)]"""
        async with self._write_lock:
            try:
                await self.conn.execute(
                    "INSERT INTO jobs (id, parent_job_id, action, params_json, requested_by, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, 'RUNNING', ?)",
                    (job_id, parent_job_id, action, json.dumps(params), requested_by, created_at),
                )
                await self.conn.executemany(
                    "INSERT INTO job_results (job_id, node_id, command_id, status) VALUES (?, ?, ?, 'PENDING')",
                    [(job_id, node_id, command_id) for node_id, command_id in targets],
                )
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    async def update_result(self, job_id: str, node_id: str, **fields: Any) -> None:
        bad = set(fields) - _RESULT_COLUMNS
        if bad:
            raise ValueError(f"unknown job_results columns: {sorted(bad)}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        async with self._write_lock:
            await self.conn.execute(
                f"UPDATE job_results SET {cols} WHERE job_id = ? AND node_id = ?",
                (*fields.values(), job_id, node_id),
            )
            await self.conn.commit()

    async def set_job_status(self, job_id: str, status: str, finished_at: str | None) -> None:
        async with self._write_lock:
            await self.conn.execute(
                "UPDATE jobs SET status = ?, finished_at = COALESCE(finished_at, ?) WHERE id = ?",
                (status, finished_at, job_id),
            )
            await self.conn.commit()

    async def recover_interrupted(self, now: str) -> list[str]:
        """기동 시 복구 (SPEC §9). RUNNING job → INTERRUPTED,
        PENDING 대상 → FAILED/INTERRUPTED (전송 전), RUNNING 대상 → UNKNOWN/INTERRUPTED (전송 후일 수 있음)."""
        async with self._write_lock:
            try:
                cur = await self.conn.execute("SELECT id FROM jobs WHERE status = 'RUNNING'")
                job_ids = [r["id"] for r in await cur.fetchall()]
                for job_id in job_ids:
                    await self.conn.execute(
                        "UPDATE job_results SET status = 'FAILED', error_type = 'INTERRUPTED',"
                        " error_message = 'console 재기동으로 전송 전 중단', finished_at = ?"
                        " WHERE job_id = ? AND status = 'PENDING'",
                        (now, job_id),
                    )
                    await self.conn.execute(
                        "UPDATE job_results SET status = 'UNKNOWN', error_type = 'INTERRUPTED',"
                        " error_message = 'console 재기동으로 결과 미확인 (전송됐을 수 있음)', finished_at = ?"
                        " WHERE job_id = ? AND status = 'RUNNING'",
                        (now, job_id),
                    )
                    await self.conn.execute(
                        "UPDATE jobs SET status = 'INTERRUPTED', finished_at = ? WHERE id = ?",
                        (now, job_id),
                    )
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise
        return job_ids

    async def insert_event(self, node_id: str, ts: str, from_status: str | None, to_status: str,
                           reasons: list[str]) -> None:
        async with self._write_lock:
            await self.conn.execute(
                "INSERT INTO node_events (node_id, ts, from_status, to_status, reasons_json) VALUES (?, ?, ?, ?, ?)",
                (node_id, ts, from_status, to_status, json.dumps(reasons, ensure_ascii=False)),
            )
            await self.conn.commit()

    async def prune_events(self, before_ts: str) -> int:
        async with self._write_lock:
            cur = await self.conn.execute("DELETE FROM node_events WHERE ts < ?", (before_ts,))
            await self.conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------ reads

    async def list_events(self, limit: int, node_id: str | None = None, before_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM node_events WHERE 1 = 1"
        args: list[Any] = []
        if node_id is not None:
            sql += " AND node_id = ?"
            args.append(node_id)
        if before_id is not None:
            sql += " AND id < ?"
            args.append(before_id)
        cur = await self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
        rows = []
        for r in await cur.fetchall():
            d = dict(r)
            d["reasons"] = json.loads(d.pop("reasons_json"))
            rows.append(d)
        return rows

    async def get_job_row(self, job_id: str) -> dict | None:
        cur = await self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_results(self, job_id: str, statuses: tuple[str, ...] | None = None) -> list[dict]:
        sql = "SELECT * FROM job_results WHERE job_id = ?"
        args: list[Any] = [job_id]
        if statuses:
            sql += f" AND status IN ({', '.join('?' for _ in statuses)})"
            args += statuses
        cur = await self.conn.execute(sql + " ORDER BY rowid", args)
        return [dict(r) for r in await cur.fetchall()]

    async def counts(self, job_id: str) -> dict[str, int]:
        cur = await self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM job_results WHERE job_id = ? GROUP BY status", (job_id,)
        )
        out = dict.fromkeys(RESULT_STATUSES, 0)
        for r in await cur.fetchall():
            out[r["status"]] = r["n"]
        return out

    async def list_jobs(self, limit: int) -> list[dict]:
        sums = ", ".join(f"SUM(r.status = '{s}') AS n_{s}" for s in RESULT_STATUSES)
        cur = await self.conn.execute(
            f"SELECT j.*, {sums} FROM jobs j LEFT JOIN job_results r ON r.job_id = j.id"
            " GROUP BY j.id ORDER BY j.created_at DESC, j.rowid DESC LIMIT ?",
            (limit,),
        )
        rows = []
        for r in await cur.fetchall():
            d = dict(r)
            d["counts"] = {s: d.pop(f"n_{s}") or 0 for s in RESULT_STATUSES}
            rows.append(d)
        return rows
