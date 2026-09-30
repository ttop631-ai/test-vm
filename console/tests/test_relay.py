"""chaos 중계 응답 코드 변환 (SPEC §8): agent 401/403은 502로 바꿔 브라우저가 세션 만료로 오인하지 않게."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app import main
from app.agent_client import CallError, ChaosResult
from app.registry import Node


class FakeClient:
    def __init__(self, result: ChaosResult) -> None:
        self.result = result

    async def chaos(self, node, body):
        return self.result


def relay(result: ChaosResult, node_id: str = "node-c"):
    main.app.state.nodes = {"node-c": Node(id="node-c", name="C", url="http://node-c:9000", token="t")}
    main.app.state.client = FakeClient(result)
    return asyncio.run(main._relay_chaos(node_id, {"reset": True}))


def test_ok_passthrough():
    resp = relay(ChaosResult(200, body={"blackhole": False}))
    assert resp.status_code == 200


def test_agent_validation_error_passthrough():
    resp = relay(ChaosResult(400, body={"detail": "bad"}))
    assert resp.status_code == 400


@pytest.mark.parametrize("code", [401, 403])
def test_agent_auth_error_becomes_502(code):
    with pytest.raises(HTTPException) as e:
        relay(ChaosResult(code, body={"detail": "invalid agent token"}))
    assert e.value.status_code == 502
    assert "AGENT_TOKEN" in e.value.detail


def test_transport_error_is_502():
    with pytest.raises(HTTPException) as e:
        relay(ChaosResult(None, error=CallError("CONNECT_ERROR", "ConnectTimeout after 1.0s")))
    assert e.value.status_code == 502


def test_unknown_node_404():
    with pytest.raises(HTTPException) as e:
        relay(ChaosResult(200, body={}), node_id="node-z")
    assert e.value.status_code == 404
