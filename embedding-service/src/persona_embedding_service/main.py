"""임베딩 서비스 FastAPI 앱 — 인수인계 §4-4.

`/healthz`는 프로세스 생존만 본다(모델 로딩 중에도 200) — liveness가 로딩 시간
동안 파드를 죽이면 복구되지 않는 재시작 루프에 빠진다. `/readyz`는 모델 로딩
완료 여부를 본다. 모델 로딩은 lifespan에서 블로킹하지 않고 백그라운드 스레드로
돌려 `/healthz`가 로딩 중에도 즉시 응답하게 한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import config
from .model import Model

# uvicorn은 자기 자신의 로거(uvicorn.access/error)만 설정하고 애플리케이션 로거는
# 건드리지 않는다 — 이걸 안 하면 root logger에 핸들러가 없어 아래 logger.info가
# 조용히 버려진다(계약이 요구하는 요청 로그가 실제로는 하나도 안 남는다).
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message


class EmbedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_type: Literal["passage", "query"]
    texts: list[str] = Field(max_length=config.MAX_TEXTS_PER_REQUEST)

    @field_validator("texts")
    @classmethod
    def texts_are_nonempty_and_within_length(cls, value: list[str]) -> list[str]:
        for text in value:
            if not text:
                raise ValueError("text must not be empty")
            if len(text) > config.MAX_CHARS_PER_TEXT:
                raise ValueError(f"text exceeds {config.MAX_CHARS_PER_TEXT} characters")
        return value


class EmbedResponse(BaseModel):
    model: str
    dim: int
    vectors: list[list[float]]


def _prefix_for(input_type: str) -> str:
    return config.PASSAGE_PREFIX if input_type == "passage" else config.QUERY_PREFIX


def _on_load_done(task: asyncio.Task) -> None:
    """모델 로딩이 백그라운드 태스크에서 실패하면 예외가 태스크 안에 갇혀 조용히
    사라진다 — `/readyz`는 로그 한 줄 없이 영원히 503만 내고, startupProbe(`/healthz`
    기준)는 통과해버려 readiness만 계속 실패하는 Pod가 남는다. 로그를 남기고
    프로세스를 종료해 k8s가 CrashLoopBackOff로 재시작하게 한다 — 스스로 복구할 방법이
    없으니 이 편이 조용히 죽어 있는 것보다 낫다.
    """
    error = task.exception()
    if error is not None:
        logger.exception("model load failed", exc_info=error)
        os._exit(1)


def create_app(model: Model | None = None) -> FastAPI:
    """`model`을 주면(테스트) 그 상태를 그대로 쓰고 lifespan이 로딩을 대신 시작하지
    않는다 — 실제 모델 없이 단위 테스트를 빠르게 돌리기 위한 주입 지점이다."""
    injected = model is not None
    app_model = model if model is not None else Model()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not injected:
            # 블로킹 로딩을 스레드로 돌려 이벤트 루프가 /healthz에 즉시 응답하게 한다.
            # 반환값을 안 잡으면 태스크가 참조 없이 떠 있다가 GC 대상이 돼 로딩 도중
            # 사라질 수 있다(CPython 공식 경고) — app.state에 보관해 막는다.
            task = asyncio.create_task(asyncio.to_thread(app_model.load))
            app.state.load_task = task
            task.add_done_callback(_on_load_done)
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.model = app_model

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, error: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status,
            content={"error": {"code": error.code, "message": error.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        # 본문(texts)을 절대 로그·응답에 남기지 않는다 — 길이·개수 위반이어도 원문은 내지 않는다.
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "invalid_request", "message": "요청이 계약을 벗어났다."}},
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        if not app_model.ready:
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return JSONResponse(status_code=200, content={"status": "ready"})

    @app.post("/embed", response_model=EmbedResponse)
    def embed(body: EmbedRequest) -> EmbedResponse:
        if not app_model.ready:
            raise ApiError(503, "model_not_ready", "모델이 아직 로딩 중입니다.")

        started = time.monotonic()
        prefix = _prefix_for(body.input_type)
        vectors = app_model.encode([prefix + text for text in body.texts])
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)

        # 본문·벡터는 절대 로그에 남기지 않는다 — 개수·최대 길이·소요 시간·input_type만.
        max_chars = max((len(text) for text in body.texts), default=0)
        logger.info(
            "embed count=%d max_chars=%d elapsed_ms=%s input_type=%s",
            len(body.texts),
            max_chars,
            elapsed_ms,
            body.input_type,
        )

        return EmbedResponse(
            model=f"{config.MODEL_NAME.rsplit('/', 1)[-1]}@{config.MODEL_REVISION}",
            dim=config.EMBEDDING_DIM,
            vectors=vectors,
        )

    return app
