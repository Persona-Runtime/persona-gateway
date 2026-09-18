from __future__ import annotations

import hmac
import logging
import time
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict
from psycopg import Error as PsycopgError
from psycopg_pool import PoolTimeout

from .config import Settings
from .cursor import CursorError, PersonaCursor, decode as decode_cursor, encode as encode_cursor
from .indexing.embedding_client import EmbeddingError, embed
from .indexing.runner import run_indexing
from .repository import (
    Draft,
    DraftAlreadyExists,
    DraftNotFound,
    DraftSettings,
    DraftValidationError,
    DuplicatePersonaName,
    IdempotencyConflict,
    IndexingInProgress,
    NoSourcesToIndex,
    NotIndexed,
    Persona,
    PersonaLimitExceeded,
    PersonaNotFound,
    RevisionConflict,
    ReadinessStore,
    PersonaStore,
    PostgresPersonaStore,
    create_pool,
)
from .retrieval.metrics import RETRIEVAL_SECONDS
from .retrieval.search import BODY_KINDS, SPEECH_KINDS, RetrievedChunk, load_indexed_version, search

logger = logging.getLogger(__name__)


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


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    profile: str
    speech_examples: str


class CreateDraftRequest(BaseModel):
    """계약의 CreateDraft. 두 경로 중 하나만 온다."""

    model_config = ConfigDict(extra="forbid")
    settings: SettingsRequest | None = None
    base_version_id: UUID | None = None


class SourceUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID | None = None
    kind: str
    filename: str | None = None
    content: str


class DraftPatchRequest(BaseModel):
    """계약의 DraftPatch. expected_revision은 항상 필요하다."""

    model_config = ConfigDict(extra="forbid")
    expected_revision: int
    settings: dict[str, str] | None = None
    upsert_sources: list[SourceUpsertRequest] = []
    remove_source_ids: list[UUID] = []


class DraftApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int


def require_idempotency_key(raw: str | None) -> UUID:
    """변경 API가 요구하는 Idempotency-Key를 UUID로 바꾼다.

    4절이 "모든 변경 API는 Idempotency-Key UUID를 요구한다"로 정해 두었다.
    변경 API가 늘어나도 같은 규칙을 쓰도록 한곳에 둔다.
    """
    if not raw:
        raise ApiError(400, "invalid_idempotency_key", "Idempotency-Key가 필요합니다.")
    try:
        return UUID(raw)
    except ValueError as error:
        raise ApiError(400, "invalid_idempotency_key", "Idempotency-Key를 확인해주세요.") from error


TRANSIENT_DATABASE_SQLSTATES = frozenset({"53300", "57P01", "57P03"})


def is_transient_database_error(error: PsycopgError) -> bool:
    sqlstate = error.sqlstate
    return sqlstate is not None and (
        sqlstate.startswith("08") or sqlstate in TRANSIENT_DATABASE_SQLSTATES
    )


def draft_summary_response(persona: Persona) -> dict[str, object] | None:
    if persona.draft is None:
        return None
    return {
        "version_id": str(persona.draft.version_id),
        "revision": persona.draft.revision,
        "status": persona.draft.status,
        "job_id": str(persona.draft.job_id) if persona.draft.job_id else None,
        # 처리기가 없으므로 항상 처리가 필요하다. Draft.requires_processing과 같은 이유다.
        "requires_processing": True,
    }


def persona_response(persona: Persona) -> dict[str, object]:
    return {
        "id": str(persona.id),
        "name": persona.name,
        "status": persona.status,
        "active_version_id": None,
        "draft": draft_summary_response(persona),
        "deletion_id": str(persona.deletion_id) if persona.deletion_id else None,
        "created_at": persona.created_at,
    }


def persona_detail_response(persona: Persona) -> dict[str, object]:
    """계약의 PersonaDetail. 목록 응답에 active_version을 더한 모양이다."""
    detail = persona_response(persona)
    # 적용본은 아직 없다. 처리·활성화가 구현되면 그때 채운다.
    detail["active_version"] = None
    return detail


def draft_response(draft: Draft) -> dict[str, object]:
    return {
        "version_id": str(draft.version_id),
        "revision": draft.revision,
        "status": draft.status,
        "job_id": str(draft.job_id) if draft.job_id else None,
        "requires_processing": draft.requires_processing,
        "persona_id": str(draft.persona_id),
        "base_version_id": str(draft.base_version_id) if draft.base_version_id else None,
        "settings": {
            "name": draft.settings.name,
            "profile": draft.settings.profile,
            "speech_examples": draft.settings.speech_examples,
        },
        "sources": [
            {
                "id": str(source.id),
                "kind": source.kind,
                "filename": source.filename,
                "content": source.content,
                "byte_size": source.byte_size,
                "sha256": source.sha256,
            }
            for source in draft.sources
        ],
        # 경고는 처리 결과가 만든다. 처리기가 없으므로 지금은 비어 있다.
        "warnings": [],
        "can_activate": draft.can_activate,
        "updated_at": draft.updated_at,
    }


def retrieve_chunk_response(chunk: RetrievedChunk) -> dict[str, object]:
    # id·source_id·char_count는 내부 식별자·서버 상태라 응답에서 뺀다.
    return {
        "kind": chunk.kind,
        "heading_path": chunk.heading_path,
        "ordinal": chunk.ordinal,
        "score": chunk.score,
        "content": chunk.content,
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
        # 이전 프로세스가 색인 도중 죽었으면 그 행이 processing에 멈춰 있다 — advisory
        # lock은 연결이 끊기면 자동으로 풀리지만(세션 범위), status는 그대로 남아 새
        # apply 요청을 "진행 중"으로 착각해 영원히 막는다. indexed_revision·indexed_at은
        # 손대지 않는다 — 죽기 전에 성공한 색인이 있었다면 그건 여전히 유효하다.
        if isinstance(store, PostgresPersonaStore):
            with store.pool.connection() as connection, connection.transaction():
                connection.execute(
                    "UPDATE persona_minimal.material_versions "
                    "SET status = 'failed', error_code = 'interrupted' "
                    "WHERE status = 'processing'"
                )
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
        # 브라우저가 Content-Type을 멋대로 추측하지 못하게 한다. 지금은 JSON만 돌려주지만,
        # 추측을 허용하면 오류 본문이나 프록시가 끼워 넣은 응답이 다른 형식으로 해석될 수 있다.
        response.headers["X-Content-Type-Options"] = "nosniff"
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

    @app.get("/metrics")
    def metrics():
        # /healthz·/readyz와 같은 운영 엔드포인트 취급 — 인증하지 않는다.
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

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
        parsed_key = require_idempotency_key(idempotency_key)
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

    def draft_error(error: Exception) -> ApiError:
        """저장소 예외를 계약의 오류로 옮긴다.

        소유권 위반과 부재는 같은 404다. 코드를 나누면 남의 캐릭터가 있는지 새어 나간다.
        """
        if isinstance(error, PersonaNotFound):
            return ApiError(404, "persona_not_found", "캐릭터를 찾을 수 없습니다.")
        if isinstance(error, DraftNotFound):
            return ApiError(404, "draft_not_found", "초안이 없습니다.")
        if isinstance(error, DraftAlreadyExists):
            return ApiError(409, "draft_exists", "이미 초안이 있습니다.")
        if isinstance(error, RevisionConflict):
            return ApiError(
                409,
                "revision_conflict",
                "그 사이에 초안이 바뀌었습니다. 다시 읽고 수정해주세요.",
            )
        if isinstance(error, IdempotencyConflict):
            return ApiError(
                409, "idempotency_conflict", "같은 키에 다른 요청을 사용할 수 없습니다."
            )
        if isinstance(error, DraftValidationError):
            return ApiError(error.status, error.code, "초안 수정을 처리할 수 없습니다.")
        raise error

    @app.get("/v1/personas/{persona_id}")
    def get_persona(
        request: Request,
        persona_id: UUID,
        user: tuple[str, str] = Depends(authenticated_user),
    ):
        try:
            persona = request.app.state.store.get_persona(user[0], persona_id)
        except Exception as error:
            raise draft_error(error) from error
        return persona_detail_response(persona)

    @app.post("/v1/personas/{persona_id}/draft", status_code=201)
    def create_draft(
        request: Request,
        persona_id: UUID,
        body: CreateDraftRequest,
        user: tuple[str, str] = Depends(authenticated_user),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        key = require_idempotency_key(idempotency_key)
        # 계약의 oneOf를 여기서도 강제한다. 스키마만 믿으면 이 서버가 직접 받는 요청은
        # 아무도 검사하지 않는다.
        if (body.settings is None) == (body.base_version_id is None):
            raise ApiError(
                422,
                "invalid_request",
                "settings 또는 base_version_id 중 하나만 보내주세요.",
            )
        if body.base_version_id is not None:
            # 적용본이 아직 없다. 있지도 않은 version에서 파생시키는 대신 분명히 알린다.
            raise ApiError(404, "version_not_found", "파생할 적용본이 없습니다.")

        settings = body.settings
        assert settings is not None  # 위 분기가 보장한다
        if not settings.name.strip() or not settings.profile.strip():
            raise ApiError(422, "invalid_settings", "이름과 소개는 비어 있을 수 없습니다.")
        try:
            draft = request.app.state.store.create_draft(
                user[0],
                persona_id,
                DraftSettings(
                    name=settings.name,
                    profile=settings.profile,
                    speech_examples=settings.speech_examples,
                ),
                key,
            )
        except Exception as error:
            raise draft_error(error) from error
        return draft_response(draft)

    @app.get("/v1/personas/{persona_id}/draft")
    def get_draft(
        request: Request,
        persona_id: UUID,
        user: tuple[str, str] = Depends(authenticated_user),
    ):
        try:
            draft = request.app.state.store.get_draft(user[0], persona_id)
        except Exception as error:
            raise draft_error(error) from error
        return draft_response(draft)

    @app.patch("/v1/personas/{persona_id}/draft")
    def patch_draft(
        request: Request,
        persona_id: UUID,
        body: DraftPatchRequest,
        user: tuple[str, str] = Depends(authenticated_user),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        require_idempotency_key(idempotency_key)
        # 계약 5절: 모두 비어 있는 변경은 422다. 아무것도 바꾸지 않으면서 revision만
        # 올리면 다른 사람의 CAS를 무의미하게 깨뜨린다.
        if body.settings is None and not body.upsert_sources and not body.remove_source_ids:
            raise ApiError(422, "empty_patch", "바꿀 내용이 없습니다.")
        try:
            draft = request.app.state.store.patch_draft(
                user[0],
                persona_id,
                body.expected_revision,
                body.settings,
                [item.model_dump() for item in body.upsert_sources],
                body.remove_source_ids,
            )
        except Exception as error:
            raise draft_error(error) from error
        return draft_response(draft)

    @app.post("/v1/personas/{persona_id}/draft/apply", status_code=202)
    def apply_draft(
        request: Request,
        persona_id: UUID,
        body: DraftApplyRequest,
        background_tasks: BackgroundTasks,
        user: tuple[str, str] = Depends(authenticated_user),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        require_idempotency_key(idempotency_key)
        # PATCH의 revision_conflict와 문구·코드를 다르게 쓰기로 했으므로(계약), 여기만
        # draft_error를 거치지 않고 먼저 잡는다. IndexingInProgress도 같은 이유로 먼저 잡는다 —
        # "진행 중" 자체가 이 엔드포인트에만 있는 개념이라 공용 매퍼에 넣을 이유가 없다.
        try:
            handle = request.app.state.store.start_indexing(
                user[0], persona_id, body.expected_revision
            )
        except RevisionConflict as error:
            raise ApiError(
                409, "revision_mismatch", "그 사이에 초안이 바뀌었습니다. 다시 읽고 적용해주세요."
            ) from error
        except IndexingInProgress as error:
            raise ApiError(409, "indexing_in_progress", "이미 색인이 진행 중입니다.") from error
        except NoSourcesToIndex as error:
            raise ApiError(422, "no_content", "색인할 자료가 없습니다.") from error
        except Exception as error:
            raise draft_error(error) from error
        background_tasks.add_task(run_indexing, handle, request.app.state.settings.embedding_url)
        return JSONResponse(
            status_code=202,
            content={"version_id": str(handle.version_id), "status": "processing"},
        )

    @app.delete("/v1/personas/{persona_id}/draft", status_code=204)
    def discard_draft(
        request: Request,
        persona_id: UUID,
        user: tuple[str, str] = Depends(authenticated_user),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        key = require_idempotency_key(idempotency_key)
        try:
            request.app.state.store.discard_draft(user[0], persona_id, key)
        except Exception as error:
            raise draft_error(error) from error
        return Response(status_code=204)

    @app.get("/v1/personas/{persona_id}/retrieve")
    def retrieve(
        request: Request,
        persona_id: UUID,
        q: str,
        k: int = 5,
        user: tuple[str, str] = Depends(authenticated_user),
    ):
        # 응답에 원문 조각이 그대로 들어간다 — 디버그 전용, 기본 꺼짐.
        if not request.app.state.settings.retrieve_debug_enabled:
            raise ApiError(404, "not_found", "찾을 수 없습니다.")
        if not (1 <= len(q) <= 2000):
            raise ApiError(422, "invalid_request", "q는 1~2000자여야 합니다.")
        if not (1 <= k <= 10):
            raise ApiError(422, "invalid_request", "k는 1~10 사이여야 합니다.")

        started = time.monotonic()
        try:
            version_id, indexed_revision = load_indexed_version(
                request.app.state.store.pool, user[0], persona_id
            )
            result = embed(request.app.state.settings.embedding_url, [q], "query")
        except PersonaNotFound as error:
            raise ApiError(404, "persona_not_found", "캐릭터를 찾을 수 없습니다.") from error
        except NotIndexed as error:
            raise ApiError(409, "not_indexed", "아직 색인된 자료가 없습니다.") from error
        except EmbeddingError as error:
            raise ApiError(
                503, "embedding_unavailable", "임베딩 서비스에 연결할 수 없습니다."
            ) from error
        query_vector = result.vectors[0]

        with request.app.state.store.pool.connection() as connection:
            with RETRIEVAL_SECONDS.labels(kind_group="body").time():
                body = search(
                    connection,
                    persona_id=persona_id,
                    version_id=version_id,
                    query_vector=query_vector,
                    kinds=BODY_KINDS,
                    k=k,
                )
            with RETRIEVAL_SECONDS.labels(kind_group="speech").time():
                speech = search(
                    connection,
                    persona_id=persona_id,
                    version_id=version_id,
                    query_vector=query_vector,
                    kinds=SPEECH_KINDS,
                    k=k,
                )
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)

        # 접근 로그에 q는 남기지 않는다 — request_id·k·조각 수·지연만.
        logger.info(
            "retrieve request_id=%s k=%d body=%d speech=%d elapsed_ms=%s",
            request.state.request_id,
            k,
            len(body),
            len(speech),
            elapsed_ms,
        )
        # indexed_revision은 마지막으로 "성공한" 색인의 revision이고, body/speech
        # 조각도 그 revision 기준이다. 사용자가 그 뒤 PATCH로 자료를 편집했다면
        # (아직 재색인 전이라면) 지금 초안의 revision은 이 값보다 클 수 있고, 조각은
        # 편집 전 내용을 반영한다. 이 엔드포인트는 그 차이를 감지·경고하지 않는다 —
        # 디버그 전용이라 호출자가 indexed_revision을 보고 스스로 판단한다.
        return {
            "indexed_revision": indexed_revision,
            "body": [retrieve_chunk_response(chunk) for chunk in body],
            "speech": [retrieve_chunk_response(chunk) for chunk in speech],
        }

    return app
