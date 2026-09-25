from __future__ import annotations

import hmac
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict
from psycopg import Error as PsycopgError
from psycopg.errors import UndefinedTable
from psycopg_pool import PoolTimeout

from .build_info import record_build_info
from .chat import service as chat_service
from .chat.fake_inference import FakeInferenceClient
from .chat.vllm_client import VllmInferenceClient
from .chat.inference import InferenceClient
from .chat.repository import (
    ChatStore,
    Citation,
    ConversationNotFound,
    Generation,
    GenerationInProgress,
    GenerationNotFound,
    IdempotencyConflict as ChatIdempotencyConflict,
    RetryNotAllowed,
)
from .chat.sse_stream import GenerationStreamResponse
from .config import Settings
from .cursor import (
    ConversationCursor,
    CursorError,
    MessageCursor,
    PersonaCursor,
    decode as decode_cursor,
    decode_conversation_cursor,
    decode_message_cursor,
    encode as encode_cursor,
    encode_conversation_cursor,
    encode_message_cursor,
)
from .http_metrics import HttpMetricsMiddleware
from .indexing.embedding_client import EmbeddingError, embed
from .indexing.runner import run_indexing
from .repository import (
    BaseVersionNotFound,
    Draft,
    DraftAlreadyExists,
    DraftNotFound,
    DraftNotStarted,
    DraftSettings,
    DraftValidationError,
    DuplicatePersonaName,
    IdempotencyConflict,
    IndexingInProgress,
    NoActiveVersion,
    NoSourcesToIndex,
    NotActivatable,
    NotIndexed,
    Persona,
    PersonaLimitExceeded,
    PersonaNotFound,
    RevisionConflict,
    SchemaNotReady,
    ReadinessStore,
    PersonaStore,
    PostgresPersonaStore,
    create_pool,
)
from .retrieval.metrics import RETRIEVAL_SECONDS
from .retrieval.search import BODY_KINDS, SPEECH_KINDS, RetrievedChunk, load_indexed_version, search

logger = logging.getLogger(__name__)

# GitHub login 형식(1~39자, 영숫자+하이픈, 앞뒤 하이픈 불가). authenticated_user가
# Traefik의 X-Auth-Request-User를 신원으로 받아들이기 전에 이 형식만 통과시킨다 —
# 개행·공백·40자 이상 값은 애초에 이 문자 클래스에 없어 걸러진다.
_GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


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


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: UUID
    message: str


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
        # 상세 응답(draft_response)과 같은 규칙으로 계산한다 — 예전엔 여기만 상수 True라
        # 같은 초안이 목록과 상세에서 다르게 보일 수 있었다.
        "requires_processing": persona.draft.requires_processing,
    }


def persona_response(persona: Persona) -> dict[str, object]:
    return {
        "id": str(persona.id),
        "name": persona.name,
        "status": persona.status,
        "active_version_id": (
            str(persona.active_version_id) if persona.active_version_id else None
        ),
        "draft": draft_summary_response(persona),
        "deletion_id": str(persona.deletion_id) if persona.deletion_id else None,
        "created_at": persona.created_at,
    }


def persona_detail_response(persona: Persona) -> dict[str, object]:
    """계약의 PersonaDetail. 목록 응답에 active_version을 더한 모양이다."""
    detail = persona_response(persona)
    # active_version_id(포인터)는 위에서 채웠지만 active_version(상세)은 계속 null이다.
    # 계약의 Version은 id·settings·sources·activated_at을 모두 요구한다. 앞의 셋은
    # 이제 적용본 version 행에 고정돼 있지만(활성화 뒤 그 행으로 가는 수정 경로가
    # 없다) activated_at 컬럼이 없다. 일부만 채워 내보내면 additionalProperties:false·
    # required를 어기므로, 그 컬럼이 생길 때까지 null을 유지한다.
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
        # 마지막으로 색인에 성공한 revision·시각. status·error_code(마지막 적용 시도
        # 결과)와 분리돼 있다 — rev4 편집·색인 실패에도 rev3 색인은 그대로 유효할 수 있다.
        "indexed_revision": draft.indexed_revision,
        "indexed_at": draft.indexed_at,
        "error_code": draft.error_code,
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
    # response_headers 미들웨어와 같은 값을 쓴다 — 예외 처리 경로가 그 미들웨어를
    # 거치기 전에 이 응답을 반환하는 경우가 있어(실측: 일반 Exception 핸들러 경로),
    # 여기서도 직접 맞춰 둬야 항상 같은 값이 나간다.
    response.headers["Cache-Control"] = "no-store, no-transform"
    response.headers["X-Request-Id"] = request_id
    return response


def build_inference_client(settings: Settings) -> InferenceClient:
    """설정값 하나로 어떤 업스트림을 쓸지 정한다. 기동 시점에 한 번만 부른다.

    llm 모드에서 연결이 안 되더라도 mock으로 바꾸지 않는다 — 계약(§8 "실제 운영 중
    LLM이 꺼졌다고 몰래 mock으로 fallback하지 않는다")이 금지하고, 그렇게 하면 합성
    응답이 진짜 답변인 것처럼 저장된다. 연결 실패는 생성 시점에 UpstreamError로 드러난다.

    llm인데 연결 설정이 비어 있는 경우는 여기까지 오지 않는다 — Settings의
    llm_settings_are_complete가 기동 시점에 이미 막는다.
    """
    if settings.chat_inference_mode == "mock":
        return FakeInferenceClient()
    # mypy·독자 모두에게: 위 validator가 보장하지만 타입상으로는 None일 수 있다.
    assert settings.vllm_base_url is not None
    assert settings.vllm_model is not None
    return VllmInferenceClient(
        base_url=settings.vllm_base_url,
        model=settings.vllm_model,
        api_key=(
            settings.vllm_api_key.get_secret_value() if settings.vllm_api_key is not None else None
        ),
        connect_timeout_seconds=settings.vllm_connect_timeout_seconds,
        first_token_timeout_seconds=settings.vllm_first_token_timeout_seconds,
        idle_timeout_seconds=settings.vllm_idle_timeout_seconds,
    )


def create_app(
    settings: Settings | None = None,
    store: PersonaStore | None = None,
    inference_client: InferenceClient | None = None,
) -> FastAPI:
    settings = settings or Settings()
    owned_pool = None
    if store is None:
        owned_pool = create_pool(settings.database_url, settings.database_timeout_seconds)
        store = PostgresPersonaStore(owned_pool)
    # generation 행의 mode는 지금 어떤 어댑터로 답하는지를 그대로 적는다 — 이 값이 SSE
    # meta.mode와 Prometheus mode 라벨이 되므로, mock과 llm을 비교하려면 사실이어야 한다.
    chat_store = (
        ChatStore(store.pool, mode=settings.chat_inference_mode)
        if isinstance(store, PostgresPersonaStore)
        else None
    )
    # 테스트가 직접 주입한 client가 있으면 그것을 쓴다. 없을 때만 설정값으로 고른다.
    owned_inference_client = None
    if inference_client is None:
        inference_client = build_inference_client(settings)
        owned_inference_client = inference_client

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # 이전 프로세스가 색인 도중 죽었으면 그 행이 processing에 멈춰 있다 — advisory
        # lock은 연결이 끊기면 자동으로 풀리지만(세션 범위), status는 그대로 남아 새
        # apply 요청을 "진행 중"으로 착각해 영원히 막는다. indexed_revision·indexed_at은
        # 손대지 않는다 — 죽기 전에 성공한 색인이 있었다면 그건 여전히 유효하다.
        #
        # 호환 창 도구(2026-09-19 0001 호환 릴리스에서 추가) — material_versions가
        # 물리적으로 없는 구 revision DB에서 이 UPDATE를 무조건 돌리면 시작 시점
        # (lifespan)에 예외가 나 앱 자체가 뜨지 못한다 — readyz조차 응답할 수 없게
        # 되어 호환 릴리스의 목적(구 revision에서도 Ready) 자체가 깨진다(실측
        # 2026-09-21: 물리적 0001 다운그레이드 테스트에서 발견). 미리 alembic_version을
        # 조회해 스키마 유무를 판정하는 대신(별도 쿼리·타임아웃 예산이 필요해지고,
        # 그 예산이 readyz 자체 예산과 겹치면 잠긴 상태에서 시작이 불필요하게 오래
        # 걸린다 — 실측으로 확인함) UPDATE를 그대로 시도하고 UndefinedTable만 잡아
        # 건너뛴다. 테이블이 잠겨 있는 경우는(이 UPDATE에 원래부터 별도 timeout이
        # 없었다) 이 변경 이전과 동일하게 둔다 — 이번 수정의 범위는 "테이블이 아예
        # 없는" 구 revision 경우로 좁힌다. 지금(호환 창을 닫은 뒤)은 SUPPORTED_
        # ALEMBIC_REVISIONS가 0003 하나뿐이라 이 except가 걸릴 일이 없는 죽은
        # 분기지만, 다음 호환 릴리스에서 재사용한다.
        if isinstance(store, PostgresPersonaStore):
            with store.pool.connection() as connection, connection.transaction():
                try:
                    # savepoint — UPDATE가 실패해도 바깥 트랜잭션은 에러 상태로 남지
                    # 않는다. psycopg3는 트랜잭션 블록 안에서 실패한 문장을 그냥
                    # try/except로 삼키기만 하면 그 블록 전체가 서버 쪽에서 에러
                    # 상태로 남아 COMMIT이 암묵적 ROLLBACK으로 바뀐다 — 지금은 이
                    # 블록에 문장이 이거 하나뿐이라 결과가 우연히 같지만, 뒤에 다른
                    # 문장이 추가되면 그것도 함께 버려진다.
                    with connection.transaction():
                        connection.execute(
                            "UPDATE persona_minimal.material_versions "
                            "SET status = 'failed', error_code = 'interrupted' "
                            "WHERE status = 'processing'"
                        )
                except UndefinedTable:
                    pass

            # 이전 프로세스가 generation 스트리밍 도중 죽었으면(SIGTERM·크래시) 그
            # 행이 queued/running/cancel_requested에 멈춰 있다 — "프로세스가 죽었으니
            # failed 처리 후 슬롯 해제"는 하지 않는다(feedback.md 명시 금지). 대신
            # reconciling으로 옮겨 "결과를 모른다"를 정직하게 남긴다. 실제 terminal
            # 전환은 별도 background sweep이 아니라, 같은 사용자의 다음 generation
            # 요청이 잠금 안에서 heartbeat_at·300초를 보고 그 자리에서 처리한다
            # (chat/repository.py의 _reject_or_resolve_active_generation).
            #
            # 0003 호환 창 동안은(채팅 스키마가 아직 없는 DB) UndefinedTable을 잡아
            # 건너뛴다 — 위 material_versions 정리와 같은 이유.
            if chat_store is not None:
                try:
                    reconciled = chat_store.reconcile_stale_generations_on_startup()
                    if reconciled:
                        logger.warning(
                            "chat reconciliation: %d stale generation(s) marked reconciling",
                            reconciled,
                        )
                except UndefinedTable:
                    pass
        yield
        if owned_pool is not None:
            owned_pool.close()
        # 앱이 만든 client만 닫는다 — 주입받은 것은 만든 쪽이 수명을 갖는다(owned_pool과
        # 같은 규칙). vLLM adapter는 HTTP 연결 풀을 쥐고 있어 정리가 필요하다.
        if owned_inference_client is not None:
            close = getattr(owned_inference_client, "close", None)
            if close is not None:
                close()

    app = FastAPI(
        title="Persona Runtime minimal API", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.store = store
    app.state.chat_store = chat_store
    app.state.inference_client = inference_client

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        # no-store가 no-cache보다 강한 지시라 SSE 스트리밍 응답에도 그대로 쓴다(중간
        # 캐시가 아예 저장하지 못하게 한다). no-transform은 프록시가 응답 바디를
        # 손대지 못하게 한다 — SSE가 중간에 재인코딩되면 이벤트 경계(빈 줄)가 깨질
        # 수 있다.
        response.headers["Cache-Control"] = "no-store, no-transform"
        # 브라우저가 Content-Type을 멋대로 추측하지 못하게 한다. 지금은 JSON만 돌려주지만,
        # 추측을 허용하면 오류 본문이나 프록시가 끼워 넣은 응답이 다른 형식으로 해석될 수 있다.
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    # 가장 바깥에 둔다(add_middleware는 나중에 추가한 것을 바깥에 놓는다). 위 헤더
    # 미들웨어와 예외 처리까지 포함한 실제 응답 시간과 상태 코드를 재기 위해서다.
    app.add_middleware(HttpMetricsMiddleware)
    record_build_info()

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

    def authenticated_user(
        authorization: str | None = Header(default=None),
        forwarded_user: str | None = Header(default=None, alias="X-Auth-Request-User"),
    ) -> tuple[str, str]:
        """인증된 사용자의 (subject, 표시 이름)을 반환한다.

        forward_auth_enabled가 켜져 있고 forwarded_user가 오면 그 값을 GitHub 로그인으로
        신뢰한다. 이 신뢰는 아래 세 조건이 모두 지켜질 때만 안전하다 — 셋 중 하나라도
        빠지면 클라이언트가 이 헤더를 직접 채워 위조할 수 있다.

        1. NetworkPolicy로 이 Gateway는 traefik 네임스페이스에서만 도달 가능하다
           (Gate 4 §3) — 클라이언트가 Gateway에 직접 헤더를 보낼 경로가 없다.
        2. Traefik의 oauth-forward Middleware가 authResponseHeaders로 이 헤더를 실제
           GitHub 인증 결과 위에 항상 덮어쓴다 — 클라이언트가 보낸 원래 값은 버려진다
           (인터넷 진입 경로, httproute-public.yaml).
        3. 내부/Serve 경로(httproute.yaml)는 oauth-forward를 거치지 않으므로, 대신
           strip-auth-header Middleware가 이 헤더를 항상 지운다 — 그 경로로는 헤더
           자체가 Gateway에 전달되지 않는다.

        플래그가 꺼져 있거나 헤더가 없으면 기존 정적 토큰 경로로 넘어간다 — 정적 토큰
        인증은 이 기능과 무관하게 계속 동작한다.
        """
        if settings.forward_auth_enabled and forwarded_user is not None:
            login = forwarded_user.strip().lower()
            if not _GITHUB_LOGIN_RE.fullmatch(login):
                raise ApiError(401, "unauthorized", "인증이 필요합니다.")
            return f"github:{login}", login

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
        if isinstance(error, DraftNotStarted):
            # 404가 아니라 409인 이유: 활성화 뒤 초안 슬롯이 빈 것은 정상 상태이고,
            # POST /draft 한 번으로 이어갈 수 있다(DraftNotStarted docstring 참고).
            return ApiError(
                409,
                "draft_not_started",
                "적용된 자료는 수정할 수 없습니다. 새 초안을 시작한 뒤 수정해주세요.",
            )
        if isinstance(error, BaseVersionNotFound):
            return ApiError(404, "version_not_found", "파생할 적용본을 찾을 수 없습니다.")
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
            return ApiError(
                error.status,
                error.code,
                "초안 수정을 처리할 수 없습니다.",
                fields=error.fields,
            )
        if isinstance(error, SchemaNotReady):
            # 2026-09-19 호환 릴리스: migration이 아직 초안 스키마를 만들지 않은
            # revision(0001)이다. 500이 아니라 재시도 가능함을 알리는 409로 답한다.
            return ApiError(
                409,
                "schema_not_ready",
                "아직 이 기능을 쓸 수 없습니다. 잠시 후 다시 시도해주세요.",
            )
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
        settings = body.settings
        if settings is not None and (not settings.name.strip() or not settings.profile.strip()):
            raise ApiError(422, "invalid_settings", "이름과 소개는 비어 있을 수 없습니다.")
        try:
            draft = request.app.state.store.create_draft(
                user[0],
                persona_id,
                (
                    DraftSettings(
                        name=settings.name,
                        profile=settings.profile,
                        speech_examples=settings.speech_examples,
                    )
                    if settings is not None
                    else None
                ),
                key,
                base_version_id=body.base_version_id,
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

    @app.post("/v1/personas/{persona_id}/draft/activate")
    def activate_draft(
        request: Request,
        persona_id: UUID,
        body: DraftApplyRequest,
        user: tuple[str, str] = Depends(authenticated_user),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        """색인이 끝난 초안을 적용본으로 세운다(계약 §6).

        apply(색인)와 다른 동작이다 — apply는 자료를 청킹·임베딩해 검색 색인을 만들고,
        activate는 그 결과를 캐릭터가 실제로 쓸 적용본으로 지정한다. 대화는 적용본이
        있어야 시작할 수 있다(§7).
        """
        require_idempotency_key(idempotency_key)
        try:
            activated = request.app.state.store.activate_draft(
                user[0], persona_id, body.expected_revision
            )
        except RevisionConflict as error:
            raise ApiError(
                409, "revision_mismatch", "그 사이에 초안이 바뀌었습니다. 다시 읽고 적용해주세요."
            ) from error
        except NotActivatable as error:
            raise ApiError(
                409,
                "not_activatable",
                "아직 활성화할 수 없습니다. 지금 내용으로 색인을 먼저 끝내주세요.",
            ) from error
        except Exception as error:
            raise draft_error(error) from error
        return {
            "persona_id": str(activated.persona_id),
            "version_id": str(activated.version_id),
            "activated_at": activated.activated_at,
        }

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
        except SchemaNotReady as error:
            raise ApiError(
                409,
                "schema_not_ready",
                "아직 이 기능을 쓸 수 없습니다. 잠시 후 다시 시도해주세요.",
            ) from error
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

    # --- Chat API ------------------------------------------------------------

    def chat_error(error: Exception) -> ApiError:
        """chat/repository.py 예외를 계약의 오류로 옮긴다. draft_error와 같은 패턴."""
        if isinstance(error, PersonaNotFound):
            return ApiError(404, "persona_not_found", "캐릭터를 찾을 수 없습니다.")
        if isinstance(error, ConversationNotFound):
            return ApiError(404, "conversation_not_found", "대화를 찾을 수 없습니다.")
        if isinstance(error, GenerationNotFound):
            return ApiError(404, "generation_not_found", "생성을 찾을 수 없습니다.")
        if isinstance(error, NoActiveVersion):
            # 계약 §7 "새 대화는 적용본이 있어야 한다". 색인만 끝난 상태와 구분한다 —
            # 화면이 "활성화하세요"와 "자료를 넣으세요"를 다르게 안내해야 한다.
            return ApiError(
                409, "no_active_version", "아직 적용본이 없습니다. 자료를 적용(활성화)해주세요."
            )
        if isinstance(error, NotIndexed):
            return ApiError(409, "not_indexed", "아직 색인된 자료가 없습니다.")
        if isinstance(error, SchemaNotReady):
            return ApiError(
                409, "schema_not_ready", "아직 이 기능을 쓸 수 없습니다. 잠시 후 다시 시도해주세요."
            )
        if isinstance(error, GenerationInProgress):
            # 계약 §7: "다른 대화에 활성 생성이 있으면 409 generation_in_progress로
            # 거절한다." 코드명은 계약이 정한 값 그대로다.
            return ApiError(409, "generation_in_progress", "이미 진행 중인 응답이 있습니다.")
        if isinstance(error, RetryNotAllowed):
            messages = {
                "retry_not_latest": "이 시도는 대화의 최신 질문이 아니라 다시 시도할 수 없습니다.",
                "retry_input_unavailable": "재사용할 입력이 없어 다시 시도할 수 없습니다.",
                "generation_in_progress": "이미 진행 중인 응답이 있습니다.",
            }
            return ApiError(
                409, error.code, messages.get(error.code, "이 시도는 다시 시도할 수 없습니다.")
            )
        if isinstance(error, ChatIdempotencyConflict):
            return ApiError(
                409, "idempotency_conflict", "같은 키에 다른 요청을 사용할 수 없습니다."
            )
        if isinstance(error, chat_service.InvalidQuestion):
            if error.code == "blank":
                return ApiError(
                    422,
                    "invalid_message",
                    "질문은 비어 있을 수 없습니다.",
                    [{"field": "message", "code": "blank"}],
                )
            return ApiError(
                422,
                "invalid_message",
                "질문은 2000자를 넘을 수 없습니다.",
                [{"field": "message", "code": "too_long"}],
            )
        raise error

    def citation_response(citation: Citation) -> dict[str, object]:
        return citation.to_json()

    def generation_response(generation: Generation) -> dict[str, object]:
        return {
            "id": str(generation.id),
            "conversation_id": str(generation.conversation_id),
            "user_message_id": str(generation.user_message_id),
            "assistant_message_id": str(generation.assistant_message_id),
            "version_id": str(generation.version_id),
            "retry_of_generation_id": (
                str(generation.retry_of_generation_id)
                if generation.retry_of_generation_id
                else None
            ),
            "mode": generation.mode,
            "status": generation.status,
            "content": generation.content,
            "citations": [citation_response(c) for c in generation.citations],
            "failure_code": generation.failure_code,
            "can_retry": generation.can_retry,
            "created_at": generation.created_at,
            "finished_at": generation.finished_at,
        }

    def conversation_response(conversation) -> dict[str, object]:
        return {
            "id": str(conversation.id),
            "persona_id": str(conversation.persona_id),
            "title": conversation.title,
            "initial_version_id": str(conversation.initial_version_id),
            "material_changed": conversation.material_changed,
            "active_generation_id": (
                str(conversation.active_generation_id)
                if conversation.active_generation_id
                else None
            ),
            "created_at": conversation.created_at,
            "updated_at": conversation.updated_at,
        }

    def user_message_response(message) -> dict[str, object]:
        return {"id": str(message.id), "content": message.content, "created_at": message.created_at}

    @app.post("/v1/personas/{persona_id}/conversations", status_code=201)
    def create_conversation(
        request: Request,
        persona_id: UUID,
        user: tuple[str, str] = Depends(authenticated_user),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        key = require_idempotency_key(idempotency_key)
        chat_store: ChatStore = request.app.state.chat_store
        try:
            conversation = chat_store.create_conversation(user[0], persona_id, key)
        except Exception as error:
            raise chat_error(error) from error
        return conversation_response(conversation)

    @app.get("/v1/personas/{persona_id}/conversations")
    def list_conversations(
        request: Request,
        persona_id: UUID,
        user: tuple[str, str] = Depends(authenticated_user),
        limit: int = 20,
        cursor: str | None = None,
    ):
        if limit < 1 or limit > 100:
            raise ApiError(400, "invalid_limit", "limit은 1에서 100 사이여야 합니다.")
        decoded = None
        if cursor is not None:
            try:
                decoded_cursor = decode_conversation_cursor(
                    cursor,
                    request.app.state.settings.cursor_signing_key.get_secret_value(),
                    user[0],
                )
                decoded = (decoded_cursor.created_at, decoded_cursor.conversation_id)
            except CursorError as exc:
                raise ApiError(400, "invalid_cursor", "cursor를 확인해주세요.") from exc
        chat_store: ChatStore = request.app.state.chat_store
        try:
            conversations = chat_store.list_conversations(user[0], persona_id, limit + 1, decoded)
        except Exception as error:
            raise chat_error(error) from error
        has_next = len(conversations) > limit
        page = conversations[:limit]
        next_cursor = None
        if has_next:
            tail = page[-1]
            next_cursor = encode_conversation_cursor(
                ConversationCursor(user[0], tail.created_at, tail.id),
                request.app.state.settings.cursor_signing_key.get_secret_value(),
            )
        return {
            "items": [conversation_response(c) for c in page],
            "next_cursor": next_cursor,
        }

    @app.get("/v1/conversations/{conversation_id}/messages")
    def list_messages(
        request: Request,
        conversation_id: UUID,
        user: tuple[str, str] = Depends(authenticated_user),
        limit: int = 20,
        cursor: str | None = None,
    ):
        if limit < 1 or limit > 100:
            raise ApiError(400, "invalid_limit", "limit은 1에서 100 사이여야 합니다.")
        decoded = None
        if cursor is not None:
            try:
                decoded_cursor = decode_message_cursor(
                    cursor,
                    request.app.state.settings.cursor_signing_key.get_secret_value(),
                    conversation_id,
                )
                decoded = (decoded_cursor.created_at, decoded_cursor.user_message_id)
            except CursorError as exc:
                raise ApiError(400, "invalid_cursor", "cursor를 확인해주세요.") from exc
        chat_store: ChatStore = request.app.state.chat_store
        try:
            turns = chat_store.list_messages(user[0], conversation_id, limit + 1, decoded)
        except Exception as error:
            raise chat_error(error) from error
        has_next = len(turns) > limit
        page = turns[:limit]
        next_cursor = None
        if has_next:
            tail = page[-1]
            next_cursor = encode_message_cursor(
                MessageCursor(conversation_id, tail.user_message.created_at, tail.user_message.id),
                request.app.state.settings.cursor_signing_key.get_secret_value(),
            )
        return {
            "items": [
                {
                    "user_message": user_message_response(turn.user_message),
                    "generations": [generation_response(g) for g in turn.generations],
                }
                for turn in page
            ],
            "next_cursor": next_cursor,
        }

    def _stream_headers(request_id: str) -> dict[str, str]:
        return {"X-Request-Id": request_id}

    @app.post("/v1/chat/completions")
    def chat_completions(
        request: Request,
        body: ChatRequest,
        user: tuple[str, str] = Depends(authenticated_user),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        key = require_idempotency_key(idempotency_key)
        try:
            question = chat_service.validate_question(body.message)
        except chat_service.InvalidQuestion as error:
            raise chat_error(error) from error

        chat_store: ChatStore = request.app.state.chat_store
        try:
            accepted = chat_service.accept_chat_completion(
                chat_store, user[0], body.conversation_id, question, key
            )
        except Exception as error:
            raise chat_error(error) from error

        if accepted.replay:
            # JSONResponse는 route가 dict를 직접 반환할 때와 달리 FastAPI의
            # jsonable_encoder를 자동으로 거치지 않는다 — datetime 같은 값이 있으면
            # 그냥 json.dumps가 TypeError를 낸다. 여기서 직접 인코딩한다.
            return JSONResponse(
                jsonable_encoder(
                    {"replayed": True, "generation": generation_response(accepted.generation)}
                ),
                headers=_stream_headers(request.state.request_id),
            )

        # persona_id는 대화에서 다시 읽는다 — accept 단계는 그걸 반환하지 않는다.
        with request.app.state.store.pool.connection() as connection:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT persona_id FROM persona_minimal.conversations WHERE id = %s",
                    (body.conversation_id,),
                )
                persona_id = cur.fetchone()[0]

        client_gone = threading.Event()
        generator = chat_service.stream_generation(
            pool=request.app.state.store.pool,
            embedding_url=request.app.state.settings.embedding_url,
            chat_store=chat_store,
            owner_subject=user[0],
            persona_id=persona_id,
            generation=accepted.generation,
            question=question,
            inference_client=request.app.state.inference_client,
            client_gone=client_gone,
        )
        # 제너레이터를 응답이 소유해 연결이 끝나면 바로 닫는다(chat/sse_stream.py 참고).
        return GenerationStreamResponse(
            generator,
            client_gone,
            media_type="text/event-stream",
            headers=_stream_headers(request.state.request_id),
        )

    @app.post("/v1/generations/{generation_id}/cancel")
    def cancel_generation(
        request: Request,
        generation_id: UUID,
        user: tuple[str, str] = Depends(authenticated_user),
    ):
        chat_store: ChatStore = request.app.state.chat_store
        try:
            generation = chat_store.request_cancel(user[0], generation_id)
        except Exception as error:
            raise chat_error(error) from error
        if generation.status == "cancel_requested":
            request.app.state.inference_client.cancel(generation_id)
        return generation_response(generation)

    @app.post("/v1/generations/{generation_id}/retry")
    def retry_generation(
        request: Request,
        generation_id: UUID,
        user: tuple[str, str] = Depends(authenticated_user),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        key = require_idempotency_key(idempotency_key)
        chat_store: ChatStore = request.app.state.chat_store
        try:
            accepted = chat_service.accept_retry(chat_store, user[0], generation_id, key)
        except Exception as error:
            raise chat_error(error) from error

        if accepted.replay:
            return JSONResponse(
                jsonable_encoder(
                    {"replayed": True, "generation": generation_response(accepted.generation)}
                ),
                headers=_stream_headers(request.state.request_id),
            )

        with request.app.state.store.pool.connection() as connection:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT persona_id FROM persona_minimal.conversations WHERE id = %s",
                    (accepted.generation.conversation_id,),
                )
                persona_id = cur.fetchone()[0]
        question = accepted.generation.input_snapshot.question  # type: ignore[union-attr]

        client_gone = threading.Event()
        generator = chat_service.stream_generation(
            pool=request.app.state.store.pool,
            embedding_url=request.app.state.settings.embedding_url,
            chat_store=chat_store,
            owner_subject=user[0],
            persona_id=persona_id,
            generation=accepted.generation,
            question=question,
            inference_client=request.app.state.inference_client,
            client_gone=client_gone,
        )
        # 제너레이터를 응답이 소유해 연결이 끝나면 바로 닫는다(chat/sse_stream.py 참고).
        return GenerationStreamResponse(
            generator,
            client_gone,
            media_type="text/event-stream",
            headers=_stream_headers(request.state.request_id),
        )

    return app
