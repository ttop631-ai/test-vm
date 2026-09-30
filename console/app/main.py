"""FastAPI 앱. lifespan에서 poller 시작/정지, 기동 시 job 복구. 인증(로그인 세션 + Basic)은 미들웨어."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .actions import CATALOG
from .agent_client import AgentClient
from .auth import (ROLE_ADMIN, ROLE_MONITOR, SESSION_COOKIE, Account, AuthMiddleware, Authenticator,
                   SecurityHeadersMiddleware, client_ip, verify_password)
from .config import DEFAULT_PASSWORDS, Settings
from .db import Database
from .jobs import JobError, JobService, utc_now_iso
from .models import (JobCreate, JobCreated, JobDetail, JobSummary, LoginRequest, NodeDetail, NodeEvent,
                     NodeView, RetryRequest)
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
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _build_authenticator(s: Settings) -> Authenticator:
    # 평문 비밀번호 env가 남아 있으면 "바꿨다고 생각했는데 기본 해시로 열리는" 사고가 나므로 기동을 거부한다.
    for legacy in ("ADMIN_PASSWORD", "MONITOR_PASSWORD"):
        if os.environ.get(legacy):
            raise RuntimeError(f"{legacy} (plaintext) is not supported; set {legacy}_HASH "
                               f"(generate: python -m app.auth hash)")
    accounts = [Account(s.admin_user, ROLE_ADMIN, s.admin_password_hash)]
    if s.monitor_user:
        accounts.append(Account(s.monitor_user, ROLE_MONITOR, s.monitor_password_hash))
    authenticator = Authenticator(
        accounts, session_ttl_sec=s.session_ttl_sec, max_failures=s.login_max_failures,
        lockout_sec=s.login_lockout_sec, cookie_secure=s.cookie_secure,
    )
    for a in accounts:
        if verify_password(DEFAULT_PASSWORDS[a.role], a.password_hash):
            log.warning("event=default_password user=%s role=%s msg='default password in use; "
                        "set %s before exposing the console'", a.username, a.role,
                        "ADMIN_PASSWORD_HASH" if a.role == ROLE_ADMIN else "MONITOR_PASSWORD_HASH")
    log.info("event=accounts_loaded users=%s", ",".join(f"{a.username}:{a.role}" for a in accounts))
    return authenticator


auth = _build_authenticator(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
app.add_middleware(AuthMiddleware, auth=auth)
# 가장 바깥 미들웨어: 인증 실패(401)·리다이렉트(302) 응답을 포함한 모든 응답에 보안 헤더 (SPEC §11)
app.add_middleware(SecurityHeadersMiddleware)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ---------------------------------------------------------------- 로그인 (SPEC §8)

@app.get("/login", include_in_schema=False)
async def login_page(request: Request):
    if auth.sessions.get(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC_DIR / "login.html", headers={"Cache-Control": "no-store"})


@app.post("/api/login")
async def login(req: LoginRequest, request: Request):
    ip = client_ip(request.scope)
    wait = auth.limiter.locked_for(ip)
    if wait:
        log.warning("event=login_rejected client=%s reason=locked retry_after=%d", ip, wait)
        return JSONResponse({"detail": f"로그인 시도가 너무 많습니다. {wait}초 후 다시 시도하세요."},
                            status_code=429, headers={"Retry-After": str(wait)})
    account = await auth.authenticate(req.username, req.password)
    if account is None:
        auth.limiter.fail(ip)
        log.warning("event=login_failed client=%s", ip)
        return JSONResponse({"detail": "아이디 또는 비밀번호가 올바르지 않습니다."}, status_code=401)
    auth.limiter.reset(ip)
    auth.sessions.delete(request.cookies.get(SESSION_COOKIE))  # 세션 고정 방지: 항상 새 토큰
    response = JSONResponse({"username": account.username, "role": account.role})
    auth.set_cookie(response, auth.sessions.create(account))
    log.info("event=login_ok user=%s role=%s client=%s", account.username, account.role, ip)
    return response


@app.post("/api/logout")
async def logout(request: Request):
    auth.sessions.delete(request.cookies.get(SESSION_COOKIE))
    response = JSONResponse({"ok": True})
    auth.clear_cookie(response)
    log.info("event=logout user=%s", request.state.user)
    return response


@app.get("/api/me")
async def me(request: Request):
    return {"username": request.state.user, "role": request.state.role}


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
    if r.status_code in (401, 403):
        # agent가 console 토큰을 거부 = 설정 오류. 그대로 넘기면 브라우저가 console 세션 만료로 오인해 로그아웃된다.
        raise HTTPException(status_code=502, detail=f"agent rejected console token (HTTP {r.status_code}); "
                                                    f"check AGENT_TOKEN for {node_id}")
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
    # AuthMiddleware가 인증된 사용자명을 넣는다. 인증 없이 여기에 도달하는 경로는 없다.
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
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
