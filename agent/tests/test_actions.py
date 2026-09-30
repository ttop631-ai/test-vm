"""agent 명령 실행 · 멱등성 · 취소 처리 · 설정 검증 (SPEC §3)."""
from __future__ import annotations

import asyncio

import pytest

from app import actions
from app.actions import CommandStore, InvalidCommand, validate
from app.simulator import RESTARTING, RUNNING, STOPPED, Simulator


@pytest.fixture(autouse=True)
def fast_actions(monkeypatch):
    # 시뮬레이션 소요 시간(1~5초)을 짧게 줄인다
    monkeypatch.setattr(actions.random, "uniform", lambda a, b: 0.3)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 멱등성

def test_same_command_id_executes_once():
    async def scenario():
        store = CommandStore(Simulator("node-x", "X", "normal"))
        task, _ = store.submit("c1", "COLLECT_DIAG", {})
        dup_task, running = store.submit("c1", "COLLECT_DIAG", {})
        first = await task
        again_task, cached = store.submit("c1", "COLLECT_DIAG", {})
        return dup_task, running, first, again_task, cached

    dup_task, running, first, again_task, cached = run(scenario())
    assert dup_task is None and running == {"command_id": "c1", "state": "RUNNING"}
    assert again_task is None and cached == first and first["exit_code"] == 0


# ---------------------------------------------------------------- 취소 (방어)

def test_cancel_during_restart_caches_failure_and_keeps_daemon_restartable():
    """실행 태스크가 취소되면 결과가 영구 RUNNING으로 남거나 데몬이 RESTARTING에 고정되면 안 된다.

    RESTARTING에 고정되면 이후 모든 재시작이 'already restarting'으로 거부된다.
    """
    async def scenario():
        sim = Simulator("node-x", "X", "normal")
        store = CommandStore(sim)
        task, _ = store.submit("c1", "RESTART_DAEMON", {"daemon": "emr-sync"})
        await asyncio.sleep(0.05)
        assert sim.daemons["emr-sync"].status == RESTARTING
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        after_cancel = sim.daemons["emr-sync"].status
        cached = store.get("c1")
        again_task, existing = store.submit("c1", "RESTART_DAEMON", {"daemon": "emr-sync"})
        t2, _ = store.submit("c2", "RESTART_DAEMON", {"daemon": "emr-sync"})
        r2 = await t2
        return after_cancel, cached, again_task, existing, r2, sim.daemons["emr-sync"].status

    after_cancel, cached, again_task, existing, r2, final = run(scenario())
    assert after_cancel == STOPPED
    assert cached is not None and cached["state"] == "DONE" and cached["exit_code"] != 0
    assert again_task is None and existing == cached  # 같은 command_id는 다시 실행하지 않는다
    assert r2["exit_code"] == 0 and final == RUNNING   # 새 명령으로는 재시작 가능


def test_cancel_other_action_caches_failure():
    async def scenario():
        store = CommandStore(Simulator("node-x", "X", "normal"))
        task, _ = store.submit("c1", "CLEAN_LOGS", {"older_than_days": 7})
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return store.get("c1")

    cached = run(scenario())
    assert cached["state"] == "DONE" and cached["exit_code"] != 0


# ---------------------------------------------------------------- 파라미터 검증

@pytest.mark.parametrize("action,params", [
    ("RM_RF", {}),
    ("CLEAN_LOGS", {"older_than_days": True}),   # bool은 int의 하위 타입
    ("CLEAN_LOGS", {"older_than_days": 7.0}),
    ("CLEAN_LOGS", {"older_than_days": "7"}),
    ("CLEAN_LOGS", {"older_than_days": 2}),
    ("RESTART_DAEMON", {}),
    ("RESTART_DAEMON", {"daemon": "bash"}),
    ("FLUSH_CACHE", {"x": 1}),
])
def test_invalid_params_rejected(action, params):
    with pytest.raises(InvalidCommand):
        validate(action, params)


def test_default_param_filled():
    assert validate("CLEAN_LOGS", {}) == {"older_than_days": 7}


# ---------------------------------------------------------------- 설정 검증

@pytest.mark.parametrize("env", [
    {"TICK_SEC": "0"},                   # 시뮬레이터 busy loop
    {"TICK_SEC": "-1"},
    {"TICK_SEC": "nan"},
    {"BLACKHOLE_MAX_HOLD_SEC": "0"},
    {"BLACKHOLE_MAX_HOLD_SEC": "inf"},
])
def test_invalid_agent_settings_rejected(monkeypatch, env):
    from app.main import Settings
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises((ValueError, RuntimeError)):
        Settings.from_env()
