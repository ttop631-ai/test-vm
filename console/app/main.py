"""FastAPI 앱. lifespan에서 poller 시작/정지, 기동 시 job 복구. HTTP Basic 인증은 미들웨어."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .actions import CATALOG
from .agent_client import AgentClient
from .auth import BasicAuthMiddleware
from .config import Settings
from .db import Database
from .jobs import JobError, JobService, utc_now_iso
from .models import (JobCreate, JobCreated, JobDetail, JobSummary, NodeDetail, NodeEvent, NodeView,
                     RetryRequest)
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


settings = Settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.admin_password == "nodewatch":
        log.warning("event=default_admin_password msg='ADMIN_PASSWORD is the default; change it before exposing the console'")
    nodes = load_nodes(settings.nodes_file)
    client = AgentClient(settings)
    db = await Database.open(settings.db_path)
    poller = Poller(nodes, client, settings, events=db)
    # 기동 시 복구: 이전 프로세스에서 끊긴 job 정리 (SPEC §9)
    interrupted = await db.recover_interrupted(utc_now_iso())
    for job_id in interrupted:
        log.warning("event=job_interrupted job_id=%s", job_id)
    jobs = JobService(db, nodes, client, settings)
    app.state.settings = settings
    app.state.nodes = nodes
    app.state.client = client
    app.state.poller = poller
    app.state.jobs = jobs
    app.state.db = db

    task = asyncio.create_task(poller.run(), name="poller")
    _background.add(task)
    task.add_done_callback(_on_background_done)
    log.info("event=console_start nodes=%s", ",".join(nodes))
    try:
        yield
    finally:
        task.cancel()
        await poller.stop()
        await jobs.stop()
        await db.close()
        await client.aclose()
        log.info("event=console_stop")


app = FastAPI(title="NodeWatch console", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# StaticFiles mount까지 보호하려면 라우터 dependency가 아니라 미들웨어여야 한다 (SPEC §8).
app.add_middleware(BasicAuthMiddleware, username=settings.admin_user, password=settings.admin_password)


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


@app.get("/api/nodes/{node_id}/chaos")
async def get_node_chaos(node_id: str):
    return await _relay_chaos(node_id, None)


@app.post("/api/nodes/{node_id}/chaos")
async def post_node_chaos(node_id: str, body: dict[str, Any] = Body(...)):
    return await _relay_chaos(node_id, body)


async def _relay_chaos(node_id: str, body: dict | None):
    """데모용 장애 주입 중계. agent 응답 코드를 그대로 돌려주고, 전송 실패는 502."""
    node = app.state.nodes.get(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"unknown node: {node_id}")
    r = await app.state.client.chaos(node, body)
    log.info("event=chaos_relay node_id=%s method=%s status=%s", node_id, "GET" if body is None else "POST",
             r.status_code)
    if r.error is not None:
        raise HTTPException(status_code=r.status_code or 502, detail=f"{r.error.type}: {r.error.message}")
    return JSONResponse(status_code=r.status_code or 200, content=r.body)


@app.get("/api/events", response_model=list[NodeEvent])
async def list_events(limit: int = Query(100, ge=1, le=500), node_id: str | None = None,
                      before_id: int | None = Query(None, ge=1)):
    """상태 전이 이벤트 (SPEC §4.4). 최신순, before_id로 이전 페이지."""
    nodes = app.state.nodes
    if node_id is not None and node_id not in nodes:
        raise HTTPException(status_code=400, detail=f"unknown node_id: {node_id}")
    rows = await app.state.db.list_events(limit, node_id, before_id)
    # 레지스트리에서 빠진 노드의 과거 이벤트는 node_id를 이름으로 쓴다.
    return [NodeEvent(**r, node_name=nodes[r["node_id"]].name if r["node_id"] in nodes else r["node_id"])
            for r in rows]


# ---------------------------------------------------------------- actions / jobs (S3)

def _requested_by(request: Request) -> str:
    # BasicAuthMiddleware가 인증된 사용자명을 넣는다. 인증 없이 여기에 도달하는 경로는 없다.
    return request.state.user


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    # SPEC §8: 허용되지 않은 요청은 400 {"detail": "..."}
    errors = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
    return JSONResponse(status_code=400, content={"detail": errors})


@app.get("/api/actions")
async def list_actions():
    return CATALOG


@app.post("/api/jobs", status_code=202, response_model=JobCreated)
async def create_job(req: JobCreate, request: Request):
    try:
        job_id = await app.state.jobs.create(req.targets, req.action, req.params, _requested_by(request))
    except JobError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return JobCreated(job_id=job_id)


@app.get("/api/jobs", response_model=list[JobSummary])
async def list_jobs(limit: int = Query(50, ge=1, le=500)):
    return await app.state.jobs.list(limit)


@app.get("/api/jobs/{job_id}", response_model=JobDetail)
async def get_job(job_id: str):
    detail = await app.state.jobs.detail(job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    return detail


@app.post("/api/jobs/{job_id}/reconcile", status_code=202)
async def reconcile_job(job_id: str):
    try:
        await app.state.jobs.start_reconcile(job_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}") from None
    return {"job_id": job_id}


@app.post("/api/jobs/{job_id}/retry", status_code=202, response_model=JobCreated)
async def retry_job(job_id: str, request: Request, req: RetryRequest | None = Body(None)):
    include_unknown = req.include_unknown if req else False
    try:
        new_id = await app.state.jobs.retry(job_id, include_unknown, _requested_by(request))
    except LookupError:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}") from None
    except JobError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return JobCreated(job_id=new_id)


# ---------------------------------------------------------------- 정적 대시보드 (S4)
# API 라우트를 모두 등록한 뒤 마지막에 mount해야 /api/*가 가려지지 않는다.
app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True), name="static")
