from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from psycopg import Error as PsycopgError
from psycopg_pool import PoolTimeout

from .config import Settings
from .cursor import CursorError, PersonaCursor, decode as decode_cursor, encode as encode_cursor
from .repository import (
    DuplicatePersonaName,
    IdempotencyConflict,
    Persona,
    PersonaLimitExceeded,
    ReadinessStore,
    PersonaStore,
    PostgresPersonaStore,
    create_pool,
)


class ApiError(Exception):
    def __init__(
        self, status: int, code: str, message: str, fields: list[dict[str, str]] | None = None
    ):
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields


class CreatePersonaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


TRANSIENT_DATABASE_SQLSTATES = frozenset({"53300", "57P01", "57P03"})


def is_transient_database_error(error: PsycopgError) -> bool:
    sqlstate = error.sqlstate
    return sqlstate is not None and (
        sqlstate.startswith("08") or sqlstate in TRANSIENT_DATABASE_SQLSTATES
    )


def persona_response(persona: Persona) -> dict[str, object]:
    return {
        "id": str(persona.id),
        "name": persona.name,
        "status": persona.status,
        "active_version_id": None,
        "draft": None,
        "deletion_id": str(persona.deletion_id) if persona.deletion_id else None,
        "created_at": persona.created_at,
    }


def error_body(error: ApiError, request_id: str) -> dict[str, object]:
    detail: dict[str, object] = {
        "code": error.code,
        "message": error.message,
        "request_id": request_id,
    }
    if error.fields:
        detail["fields"] = error.fields
    return {"error": detail}


def error_response(request: Request, error: ApiError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or str(uuid4())
    response = JSONResponse(status_code=error.status, content=error_body(error, request_id))
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Request-Id"] = request_id
    return response


def create_app(settings: Settings | None = None, store: PersonaStore | None = None) -> FastAPI:
    settings = settings or Settings()
    owned_pool = None
    if store is None:
        owned_pool = create_pool(settings.database_url, settings.database_timeout_seconds)
        store = PostgresPersonaStore(owned_pool)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if owned_pool is not None:
            owned_pool.close()

    app = FastAPI(
        title="Persona Runtime minimal API", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.store = store

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError):
        return error_response(request, error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _: RequestValidationError):
        return error_response(request, ApiError(422, "invalid_request", "요청 값을 확인해주세요."))

    @app.exception_handler(PoolTimeout)
    async def exhausted_database_pool(request: Request, _: Exception):
        return error_response(
            request,
            ApiError(503, "dependency_unavailable", "잠시 후 다시 시도해주세요."),
        )

    @app.exception_handler(PsycopgError)
    async def database_error(request: Request, error: PsycopgError):
        # 연결 단절·대기처럼 재시도 가능한 DB 상태만 503으로 알리고, 설정·SQL 결함은 500으로 숨긴다.
        if is_transient_database_error(error):
            return error_response(
                request,
                ApiError(503, "dependency_unavailable", "잠시 후 다시 시도해주세요."),
            )
        return error_response(request, ApiError(500, "internal_error", "서버 오류가 발생했습니다."))

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, _: Exception):
        return error_response(request, ApiError(500, "internal_error", "서버 오류가 발생했습니다."))

    def authenticated_user(authorization: str | None = Header(default=None)) -> tuple[str, str]:
        expected = settings.static_bearer_token.get_secret_value()
        parts = authorization.split() if authorization else []
        if (
            len(parts) != 2
            or parts[0].lower() != "bearer"
            or not hmac.compare_digest(parts[1].encode("utf-8"), expected.encode("utf-8"))
        ):
            raise ApiError(401, "unauthorized", "인증이 필요합니다.")
        return settings.static_user_id, settings.static_display_name

    @app.get("/healthz")
    def healthz():
        # 프로세스 생존 신호다. DB가 중단돼도 재시작 루프에 빠지지 않도록 검사하지 않는다.
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(request: Request):
        candidate = request.app.state.store
        ready = isinstance(candidate, ReadinessStore) and candidate.is_ready()
        if not ready:
            # 연결 문자열·DB 예외를 응답에 넣지 않아 Secret과 내부 구조를 보호한다.
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return {"status": "ready"}

    @app.get("/v1/me")
    def get_me(user: tuple[str, str] = Depends(authenticated_user)):
        return {"id": user[0], "display_name": user[1]}

    @app.get("/v1/personas")
    def list_personas(
        request: Request,
        user: tuple[str, str] = Depends(authenticated_user),
        limit: int = 20,
        cursor: str | None = None,
    ):
        if limit < 1 or limit > 100:
            raise ApiError(400, "invalid_limit", "limit은 1에서 100 사이여야 합니다.")
        decoded_cursor = None
        if cursor is not None:
            try:
                decoded_cursor = decode_cursor(
                    cursor, settings.cursor_signing_key.get_secret_value(), user[0]
                )
            except CursorError as exc:
                raise ApiError(400, "invalid_cursor", "cursor를 확인해주세요.") from exc
        personas = request.app.state.store.list_personas(user[0], limit + 1, decoded_cursor)
        has_next = len(personas) > limit
        page = personas[:limit]
        next_cursor = None
        if has_next:
            tail = page[-1]
            next_cursor = encode_cursor(
                PersonaCursor(user[0], tail.created_at, tail.id),
                settings.cursor_signing_key.get_secret_value(),
            )
        return {
            "items": [persona_response(persona) for persona in page],
            "next_cursor": next_cursor,
        }

    @app.post("/v1/personas", status_code=201)
    def create_persona(
        request: Request,
        body: CreatePersonaRequest,
        user: tuple[str, str] = Depends(authenticated_user),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        try:
            parsed_key = UUID(idempotency_key) if idempotency_key else None
        except ValueError as exc:
            raise ApiError(
                400, "invalid_idempotency_key", "Idempotency-Key를 확인해주세요."
            ) from exc
        if parsed_key is None:
            raise ApiError(400, "invalid_idempotency_key", "Idempotency-Key가 필요합니다.")
        name = body.name.strip()
        if not name:
            raise ApiError(
                422,
                "invalid_persona_name",
                "캐릭터 이름은 비어 있을 수 없습니다.",
                [{"field": "name", "code": "blank"}],
            )
        if "\x00" in name:
            raise ApiError(
                422,
                "invalid_persona_name",
                "캐릭터 이름에 사용할 수 없는 문자가 있습니다.",
                [{"field": "name", "code": "contains_nul"}],
            )
        try:
            persona = request.app.state.store.create_persona(user[0], user[1], name, parsed_key)
        except DuplicatePersonaName as exc:
            raise ApiError(409, "duplicate_persona_name", "같은 이름의 캐릭터가 있습니다.") from exc
        except PersonaLimitExceeded as exc:
            raise ApiError(
                409, "persona_limit_exceeded", "캐릭터는 최대 3개까지 만들 수 있습니다."
            ) from exc
        except IdempotencyConflict as exc:
            raise ApiError(
                409, "idempotency_conflict", "같은 키에 다른 요청을 사용할 수 없습니다."
            ) from exc
        return persona_response(persona)

    return app
