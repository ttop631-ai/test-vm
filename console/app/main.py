"""FastAPI 앱. lifespan에서 poller 시작/정지.

S2 범위: /healthz, GET /api/nodes, GET /api/nodes/{node_id}.
job 복구(S3), 인증 미들웨어(S5), 정적 대시보드(S4)는 이후 단계에서 추가한다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from .agent_client import AgentClient
from .config import Settings
from .models import NodeDetail, NodeView
from .poller import Poller
from .registry import load_nodes

log = logging.getLogger("console.main")


def _configure_logging() -> None:
    fmt = logging.Formatter(
        "ts=%(asctime)s level=%(levelname)s logger=%(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    fmt.converter = time.gmtime
    handler = logging.StreamHandler()
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    # httpx는 요청마다 INFO 로그를 남기므로 poller 로그와 중복된다.
    logging.getLogger("httpx").setLevel(logging.WARNING)


_configure_logging()

# 백그라운드 태스크 참조 보관 (CLAUDE.md §4-5)
_background: set[asyncio.Task] = set()


def _on_background_done(task: asyncio.Task) -> None:
    _background.discard(task)
    if task.cancelled():
        return
    if task.exception() is not None:
        log.error("event=background_task_error task=%s error=%r", task.get_name(), task.exception())


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    nodes = load_nodes(settings.nodes_file)
    client = AgentClient(settings)
    poller = Poller(nodes, client, settings)
    app.state.settings = settings
    app.state.poller = poller

    task = asyncio.create_task(poller.run(), name="poller")
    _background.add(task)
    task.add_done_callback(_on_background_done)
    log.info("event=console_start nodes=%s", ",".join(nodes))
    try:
        yield
    finally:
        task.cancel()
        await poller.stop()
        await client.aclose()
        log.info("event=console_stop")


app = FastAPI(title="NodeWatch console", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/api/nodes", response_model=list[NodeView])
async def list_nodes():
    return app.state.poller.views()


@app.get("/api/nodes/{node_id}", response_model=NodeDetail)
async def get_node(node_id: str):
    poller: Poller = app.state.poller
    if node_id not in poller.states:
        raise HTTPException(status_code=404, detail=f"unknown node: {node_id}")
    return poller.detail(node_id)
