"""Mock agent: 라우트, 토큰 검사 (SPEC §3)."""
from __future__ import annotations

import asyncio
import hmac
import logging
import math
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .actions import CommandStore, InvalidCommand, validate
from .chaos import Chaos, ChaosUpdate
from .simulator import Simulator

log = logging.getLogger("agent.main")

TOKEN_HEADER = "X-Agent-Token"
PUBLIC_PATHS = {"/livez"}


@dataclass(frozen=True)
class Settings:
    node_id: str
    node_name: str
    agent_token: str
    profile: str
    tick_sec: float
    blackhole_max_hold_sec: float

    @classmethod
    def from_env(cls) -> Settings:
        token = os.environ.get("AGENT_TOKEN", "")
        if not token:
            raise RuntimeError("AGENT_TOKEN is required")
        tick_sec = float(os.environ.get("TICK_SEC", "2"))
        hold_sec = float(os.environ.get("BLACKHOLE_MAX_HOLD_SEC", "60"))
        # 0 이하·NaN·무한대는 기동 거부 (TICK_SEC=0이면 시뮬레이터 busy loop) (SPEC §3.1)
        for name, value in (("TICK_SEC", tick_sec), ("BLACKHOLE_MAX_HOLD_SEC", hold_sec)):
            if not math.isfinite(value) or value <= 0:
                raise RuntimeError(f"{name} must be a positive finite number, got {value!r}")
        return cls(
            node_id=os.environ.get("NODE_ID", "node-x"),
            node_name=os.environ.get("NODE_NAME", os.environ.get("NODE_ID", "node-x")),
            agent_token=token,
            profile=os.environ.get("PROFILE", "normal"),
            tick_sec=tick_sec,
            blackhole_max_hold_sec=hold_sec,
        )


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
    # uvicorn 기본 핸들러를 걷어내고 같은 key=value 형식으로 통일한다.
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    # access log는 auth_and_log 미들웨어가 key=value로 남긴다.
    logging.getLogger("uvicorn.access").disabled = True


_configure_logging()
settings = Settings.from_env()
sim = Simulator(node_id=settings.node_id, node_name=settings.node_name, profile=settings.profile)
chaos = Chaos(node_id=settings.node_id, blackhole_max_hold_sec=settings.blackhole_max_hold_sec)
store = CommandStore(sim)

# 백그라운드 태스크 참조 보관 (CLAUDE.md §4-5)
_background: set[asyncio.Task] = set()


def _on_background_done(task: asyncio.Task) -> None:
    _background.discard(task)
    if task.cancelled():
        return
    if task.exception() is not None:
        log.error("event=background_task_error node_id=%s task=%s error=%r",
                  settings.node_id, task.get_name(), task.exception())


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(sim.run(settings.tick_sec), name="simulator-tick")
    _background.add(task)
    task.add_done_callback(_on_background_done)
    log.info("event=agent_start node_id=%s profile=%s tick_sec=%s",
             settings.node_id, settings.profile, settings.tick_sec)
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        log.info("event=agent_stop node_id=%s", settings.node_id)


app = FastAPI(title="NodeWatch agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def auth_and_log(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS:
        return await call_next(request)
    started = time.monotonic()
    supplied = request.headers.get(TOKEN_HEADER, "")
    if not hmac.compare_digest(supplied.encode(), settings.agent_token.encode()):
        log.warning("event=auth_failed node_id=%s method=%s path=%s client=%s",
                    settings.node_id, request.method, path, request.client.host if request.client else "-")
        return JSONResponse(status_code=401, content={"detail": "invalid agent token"})
    response = await call_next(request)
    log.info("event=request node_id=%s method=%s path=%s status=%d duration_ms=%d",
             settings.node_id, request.method, path, response.status_code,
             int((time.monotonic() - started) * 1000))
    return response


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    # 형식 오류도 "허용되지 않은 요청"으로 400 처리 (SPEC §6: 400 → AGENT_ERROR)
    errors = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
    return JSONResponse(status_code=400, content={"detail": errors})


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: uuid.UUID
    action: str
    params: dict = Field(default_factory=dict)


@app.get("/livez")
async def livez():
    return {"status": "ok"}


@app.get("/health")
async def health():
    chaos.maybe_fail("/health")
    await chaos.before_response("/health")
    return sim.health()


@app.post("/commands")
async def post_command(req: CommandRequest):
    command_id = str(req.command_id)
    try:
        params = validate(req.action, req.params)
    except InvalidCommand as e:
        log.warning("event=command_rejected node_id=%s command_id=%s action=%s reason=%r",
                    settings.node_id, command_id, req.action, str(e))
        raise HTTPException(status_code=400, detail=str(e)) from None

    chaos.maybe_fail("/commands")  # 실행 전에 500

    task, existing = store.submit(command_id, req.action, params)
    if task is not None:
        # shield: 요청 코루틴이 취소돼도 실행은 끝까지 진행되고 결과가 캐시에 남는다.
        result = await asyncio.shield(task)
    else:
        result = existing

    # blackhole: 실행은 이미 끝났고 응답만 보류한다.
    await chaos.before_response("/commands")
    return result


@app.get("/commands/{command_id}")
async def get_command(command_id: str):
    chaos.maybe_fail("/commands/{command_id}")
    await chaos.before_response("/commands/{command_id}")
    result = store.get(command_id)
    if result is None:
        raise HTTPException(status_code=404, detail="command not found")
    return result


@app.get("/chaos")
async def get_chaos():
    return chaos.state()


@app.post("/chaos")
async def post_chaos(upd: ChaosUpdate):
    return chaos.apply(upd, sim)
