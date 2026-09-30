"""job 상태 갱신 경합 · 상세 집계 일관성 · 이력 조회 (SPEC §8, §9)."""
from __future__ import annotations

import asyncio
from collections import Counter

from app.config import Settings
from app.db import Database
from app.jobs import JobService

S = Settings(_env_file=None)


def run(coro):
    return asyncio.run(coro)


async def open_db(tmp_path) -> Database:
    return await Database.open(tmp_path / "t.db")


async def make_job(db: Database, job_id: str, nodes: list[str], created_at: str = "2026-10-01T00:00:00Z") -> None:
    await db.create_job(job_id, None, "COLLECT_DIAG", {}, "admin", created_at,
                        [(n, f"{job_id}-{n}") for n in nodes])


# ---------------------------------------------------------------- 상태 역행

def test_overlapping_refresh_does_not_regress_completed(tmp_path, monkeypatch):
    """먼저 시작한 갱신이 오래된 집계(RUNNING)를 늦게 쓰면서 COMPLETED를 덮어쓰면 안 된다.

    재현 경로: job 실행 중 사용자가 reconcile → 두 갱신이 겹침.
    """
    async def scenario():
        db = await open_db(tmp_path)
        svc = JobService(db, nodes={}, client=None, settings=S)
        await make_job(db, "j", ["a", "b"])
        await db.update_result("j", "a", status="SUCCESS")
        await db.update_result("j", "b", status="RUNNING")

        original = Database.counts
        gate = asyncio.Event()
        calls = 0

        async def slow_counts(self, job_id):
            nonlocal calls
            calls += 1
            result = await original(self, job_id)
            if calls == 1:  # 첫 갱신만 집계를 읽은 뒤 멈춘다 (늦게 도착하는 오래된 집계)
                try:
                    await asyncio.wait_for(gate.wait(), 0.3)
                except TimeoutError:
                    pass
            return result

        monkeypatch.setattr(Database, "counts", slow_counts)
        stale = asyncio.create_task(svc._refresh_status("j"))
        await asyncio.sleep(0.05)
        await db.update_result("j", "b", status="SUCCESS")  # 마지막 대상 완료
        await svc._refresh_status("j")                      # 최신 집계로 갱신
        gate.set()
        await stale
        status = (await db.get_job_row("j"))["status"]
        await db.close()
        return status

    assert run(scenario()) == "COMPLETED"


def test_interrupted_is_kept_on_refresh(tmp_path):
    async def scenario():
        db = await open_db(tmp_path)
        svc = JobService(db, nodes={}, client=None, settings=S)
        await make_job(db, "j", ["a"])
        await db.update_result("j", "a", status="RUNNING")
        await db.recover_interrupted("2026-10-01T00:00:01Z")
        await db.update_result("j", "a", status="SUCCESS", reconciled=1)
        await svc._refresh_status("j")
        status = (await db.get_job_row("j"))["status"]
        await db.close()
        return status

    assert run(scenario()) == "INTERRUPTED"


# ---------------------------------------------------------------- 상세 집계 일관성

def test_detail_counts_match_returned_results(tmp_path, monkeypatch):
    """결과 목록과 counts는 같은 시점이어야 한다 (사이에 결과가 바뀌어도)."""
    async def scenario():
        db = await open_db(tmp_path)
        svc = JobService(db, nodes={}, client=None, settings=S)
        await make_job(db, "j", ["a", "b"])
        await db.update_result("j", "a", status="SUCCESS")
        await db.update_result("j", "b", status="RUNNING")

        original = Database.get_results

        async def results_then_change(self, job_id, statuses=None):
            rows = await original(self, job_id, statuses)
            await Database.update_result(self, "j", "b", status="SUCCESS")  # 조회 직후 결과 변경
            return rows

        monkeypatch.setattr(Database, "get_results", results_then_change)
        detail = await svc.detail("j")
        await db.close()
        return detail

    d = run(scenario())
    shown = Counter(r.status for r in d.results)
    assert {k: v for k, v in d.counts.items() if v} == dict(shown)


# ---------------------------------------------------------------- 이력 조회

def test_list_jobs_order_limit_and_counts(tmp_path):
    async def scenario():
        db = await open_db(tmp_path)
        svc = JobService(db, nodes={}, client=None, settings=S)
        await make_job(db, "old", ["a"], "2026-10-01T00:00:00Z")
        await make_job(db, "tie1", ["a", "b"], "2026-10-01T00:00:05Z")  # 같은 초에 생성된 두 job
        await make_job(db, "tie2", ["a", "b", "c"], "2026-10-01T00:00:05Z")
        await db.update_result("tie2", "a", status="SUCCESS")
        await db.update_result("tie2", "b", status="UNKNOWN")
        rows = await svc.list(2)
        await db.close()
        return rows

    rows = run(scenario())
    assert [r.job_id for r in rows] == ["tie2", "tie1"]  # 최신순, 같은 초면 나중에 만든 것 먼저
    assert rows[0].counts == {"PENDING": 1, "RUNNING": 0, "SUCCESS": 1, "FAILED": 0, "UNKNOWN": 1}
    assert rows[1].counts["PENDING"] == 2


def test_list_jobs_cost_does_not_grow_with_history(tmp_path):
    """최신 N개 조회 비용이 전체 이력 크기에 비례하면 안 된다 (SQLite VM 단계 수로 측정)."""
    async def steps_for(total: int) -> int:
        db = await Database.open(tmp_path / f"h{total}.db")
        await db.conn.executemany(
            "INSERT INTO jobs (id, action, params_json, requested_by, status, created_at) VALUES (?, 'X', '{}', 'a', 'COMPLETED', ?)",
            [(f"j{i}", f"2026-10-01T{i // 3600 % 24:02d}:{i // 60 % 60:02d}:{i % 60:02d}Z") for i in range(total)],
        )
        await db.conn.executemany(
            "INSERT INTO job_results (job_id, node_id, command_id, status) VALUES (?, ?, ?, 'SUCCESS')",
            [(f"j{i}", n, f"c{i}{n}") for i in range(total) for n in ("a", "b", "c")],
        )
        await db.conn.commit()
        counter = 0

        def tick():
            nonlocal counter
            counter += 1
            return 0

        await db.conn.set_progress_handler(tick, 100)
        rows = await db.list_jobs(3)
        await db.conn.set_progress_handler(None, 0)
        await db.close()
        assert len(rows) == 3
        return counter

    small, large = run(steps_for(1000)), run(steps_for(2000))
    assert large < small * 1.3, f"list_jobs cost grows with history: {small} -> {large} (x100 VM steps)"
