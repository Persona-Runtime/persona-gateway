"""요청 검증·상태 전이·6단계 처리 순서의 오케스트레이션.

`main.py`의 라우트는 이 모듈의 함수를 얇게 호출하기만 한다(기존 관례 — 인증·검증·
DB 처리·응답 변환을 역할로 나눈다). 저수준 SQL은 `chat/repository.py`, 순수 SSE
포맷팅은 `chat/sse.py`, RAG는 기존 `retrieval/*`를 그대로 쓴다.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Iterator
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ..indexing.embedding_client import EmbeddingError
from ..repository import NotIndexed, PersonaNotFound, SchemaNotReady
from ..retrieval.prompt import BUDGET_8192, QuestionTooLong, build_messages
from ..retrieval.search import RetrievedChunk, retrieve_context
from . import metrics
from .fake_inference import UpstreamError
from .inference import InferenceClient
from .repository import (
    ChatStore,
    Citation,
    Generation,
    InputSnapshot,
)
from .sse import format_citations, format_delta, format_done, format_error, format_meta

MIN_QUESTION_CHARS = 1
MAX_QUESTION_CHARS = 2000
MAX_ANSWER_TOKENS = 512
# 계약값(§0) — 접수 시점 기준. FakeInferenceClient를 쓰는 테스트는 이 값을 아주
# 작게(예: 0.05초) 오버라이드해 실제로 60~180초를 기다리지 않고 판정 로직을 검증한다.
FIRST_TOKEN_DEADLINE_SECONDS = 60.0
TOTAL_GENERATION_DEADLINE_SECONDS = 180.0


class InvalidQuestion(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def validate_question(raw: str) -> str:
    question = raw.strip()
    if not question:
        raise InvalidQuestion("blank")
    # 계약은 "1~2000 Unicode 코드 포인트"다 — len()은 코드포인트를 센다(UTF-8
    # 바이트가 아니다), MAX_PROFILE_CHARS와 같은 근거.
    if len(question) > MAX_QUESTION_CHARS:
        raise InvalidQuestion("too_long")
    return question


@dataclass(frozen=True)
class ChatCompletionAccepted:
    generation: Generation
    replay: bool


def accept_chat_completion(
    chat_store: ChatStore,
    owner_subject: str,
    conversation_id: UUID,
    question: str,
    idempotency_key: UUID,
) -> ChatCompletionAccepted:
    """처리 순서 1~5단계 — 트랜잭션 하나로 끝난다. 검색(6단계)은 여기 없다."""
    generation, replay = chat_store.create_generation_queued(
        owner_subject, conversation_id, question, idempotency_key
    )
    return ChatCompletionAccepted(generation=generation, replay=replay)


@dataclass(frozen=True)
class RetryAccepted:
    generation: Generation
    replay: bool


def accept_retry(
    chat_store: ChatStore, owner_subject: str, generation_id: UUID, idempotency_key: UUID
) -> RetryAccepted:
    generation, replay = chat_store.create_retry_generation(
        owner_subject, generation_id, idempotency_key
    )
    return RetryAccepted(generation=generation, replay=replay)


def _persona_settings(pool: ConnectionPool, persona_id: UUID) -> tuple[str, str]:
    """build_messages에 필요한 settings_name·settings_profile만 가볍게 읽는다.

    get_persona처럼 draft 전체(자료·상태)를 조립하지 않는다 — 스트리밍 시작 지연을
    줄이려는 목적이라 여기서 필요한 두 컬럼만 본다.
    """
    with pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT settings_name, settings_profile FROM persona_minimal.material_versions "
                "WHERE persona_id = %s",
                (persona_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise PersonaNotFound
    return row["settings_name"], row["settings_profile"]


def _citations_from_chunks(version_id: UUID, chunks: list[RetrievedChunk]) -> list[Citation]:
    # title은 별도 컬럼이 없다 — kind·heading_path로 사람이 읽을 라벨을 만든다.
    # heading_path가 비어 있으면(예: speech_examples는 대체로 없다) kind만 쓴다.
    labels = {
        "events": "사건",
        "relationships": "관계",
        "abilities": "능력",
        "speech_examples": "말투 예시",
    }
    citations = []
    for chunk in chunks:
        label = labels.get(chunk.kind, chunk.kind)
        title = f"{label} · {chunk.heading_path}" if chunk.heading_path else label
        citations.append(
            Citation(
                id=chunk.id,
                source_id=chunk.source_id,
                version_id=version_id,
                title=title,
                excerpt=chunk.content[:200],
            )
        )
    return citations


def stream_generation(
    *,
    pool: ConnectionPool,
    embedding_url: str,
    chat_store: ChatStore,
    owner_subject: str,
    persona_id: UUID,
    generation: Generation,
    question: str,
    inference_client: InferenceClient,
    first_token_deadline_seconds: float = FIRST_TOKEN_DEADLINE_SECONDS,
    total_deadline_seconds: float = TOTAL_GENERATION_DEADLINE_SECONDS,
) -> Iterator[str]:
    """6단계(검색 → citations → delta* → done/error)를 SSE 문자열로 돌린다.

    DB에 완료 상태를 먼저 저장하고 그다음 done/error를 보낸다(feedback.md 명시
    순서) — 클라이언트가 done을 본 시점엔 이미 DB에서도 같은 상태를 볼 수 있다.

    클라이언트가 스트림 도중 연결을 끊으면 Starlette가 이 제너레이터에
    `GeneratorExit`을 던진다 — "프로세스가 안 죽었어도 결과를 모른다"를 그대로
    반영해 reconciling으로 남긴다(연결 종료만으로 슬롯을 즉시 해제하지 않는다).
    meta를 보내는 시점부터 이미 이 처리가 필요하다 — meta 직후(첫 delta조차 오기
    전에) 끊기는 것도 "결과를 모르는" 경우이므로, try는 meta yield까지 감싼다.
    """
    metrics.GENERATIONS_STARTED.labels(mode=generation.mode).inc()
    start = time.monotonic()
    try:
        yield format_meta(
            generation_id=generation.id,
            conversation_id=generation.conversation_id,
            user_message_id=generation.user_message_id,
            assistant_message_id=generation.assistant_message_id,
            version_id=generation.version_id,
            mode=generation.mode,
        )
        yield from _run_generation(
            pool=pool,
            embedding_url=embedding_url,
            chat_store=chat_store,
            owner_subject=owner_subject,
            persona_id=persona_id,
            generation=generation,
            question=question,
            inference_client=inference_client,
            first_token_deadline_seconds=first_token_deadline_seconds,
            total_deadline_seconds=total_deadline_seconds,
            start=start,
        )
    except GeneratorExit:
        chat_store.mark_generation_reconciling(generation.id)
        raise


def _run_generation(
    *,
    pool: ConnectionPool,
    embedding_url: str,
    chat_store: ChatStore,
    owner_subject: str,
    persona_id: UUID,
    generation: Generation,
    question: str,
    inference_client: InferenceClient,
    first_token_deadline_seconds: float,
    total_deadline_seconds: float,
    start: float,
) -> Iterator[str]:
    try:
        if generation.input_snapshot is not None:
            # retry — 검색을 다시 돌리지 않고 얼려둔 입력을 그대로 쓴다.
            snapshot = generation.input_snapshot
            messages = snapshot.messages
            citations = snapshot.citations
        else:
            context = retrieve_context(
                pool,
                embedding_url,
                owner_subject=owner_subject,
                persona_id=persona_id,
                question=question,
            )
            settings_name, settings_profile = _persona_settings(pool, persona_id)
            history = chat_store.load_history(
                generation.conversation_id, before_user_message_id=generation.user_message_id
            )
            result = build_messages(
                settings_name=settings_name,
                settings_profile=settings_profile,
                speech_chunks=context.speech,
                body_chunks=context.body,
                history=history,
                question=question,
                budget=BUDGET_8192,
            )
            messages = result.messages
            citations = _citations_from_chunks(
                generation.version_id, result.body_chunks_used + result.speech_chunks_used
            )
            snapshot = InputSnapshot(
                question=question,
                citations=citations,
                version_id=generation.version_id,
                messages=messages,
            )
            chat_store.mark_generation_running(generation.id, snapshot)

        yield format_citations(generation_id=generation.id, items=[c.to_json() for c in citations])
    except (PersonaNotFound, NotIndexed, SchemaNotReady, QuestionTooLong, EmbeddingError) as error:
        code = type(error).__name__
        # finish_generation이 저장된 실제 status를 돌려준다 — 검색 중에 별도 요청이
        # cancel_requested를 남겼다면(이 실패 코드가 아니라) cancelled로 저장되고,
        # 아래 이벤트도 그 실제 값을 따른다(DB와 클라이언트가 보는 결과를 일치시킨다).
        persisted = chat_store.finish_generation(
            generation.id, status="failed", content="", failure_code=code
        )
        metrics.GENERATIONS_FINISHED.labels(
            mode=generation.mode, terminal_reason=persisted.status
        ).inc()
        yield _terminal_event(persisted, fallback_finish_reason="stop")
        return

    content_parts: list[str] = []
    index = 0
    finish_reason = "stop"
    terminal_status = "completed"
    failure_code: str | None = None
    first_chunk_at: float | None = None

    # 동기 코드에서 블로킹 이터레이터(inference_client.start())를 선점형으로
    # 끊으려면 별도 스레드가 필요하다 — queue.get(timeout=...)은 워커가 next() 안에서
    # 블록 중이어도 정확히 그 시간에 반환되지만, 기존의 for 루프 조건 체크는 새
    # chunk가 와야만 실행돼 업스트림이 조용히 멈추면 영원히 대기했다(고쳐지기 전
    # 버그). 워커는 daemon 스레드다 — 이번 범위엔 실제 vLLM이 없어 무한 대기해도
    # DB 커넥션 등 자원을 쥐고 있지 않으므로 프로세스 종료를 막지 않는 daemon으로
    # 충분하다.
    chunk_queue: queue.Queue[tuple[str, object]] = queue.Queue()

    def _drain() -> None:
        try:
            for piece in inference_client.start(
                generation.id, messages, max_tokens=MAX_ANSWER_TOKENS
            ):
                chunk_queue.put(("chunk", piece))
            chunk_queue.put(("end", None))
        except Exception as error:  # UpstreamError 등 — 메인 스레드에서 다시 던진다
            chunk_queue.put(("error", error))

    threading.Thread(target=_drain, daemon=True).start()

    try:
        while True:
            now = time.monotonic()
            deadline = (
                first_token_deadline_seconds if first_chunk_at is None else total_deadline_seconds
            )
            remaining = max(0.0, deadline - (now - start))
            try:
                kind, payload = chunk_queue.get(timeout=remaining)
            except queue.Empty:
                inference_client.cancel(generation.id)
                terminal_status = "failed"
                failure_code = (
                    "first_token_timeout" if first_chunk_at is None else "generation_timeout"
                )
                finish_reason = "length"
                break
            if kind == "end":
                break
            if kind == "error":
                raise payload  # type: ignore[misc]  # UpstreamError면 아래 except가 잡는다
            if first_chunk_at is None:
                first_chunk_at = time.monotonic()
                metrics.TIME_TO_FIRST_TOKEN_SECONDS.observe(first_chunk_at - start)
            content_parts.append(payload)  # type: ignore[arg-type]
            yield format_delta(generation_id=generation.id, index=index, text=payload)  # type: ignore[arg-type]
            index += 1
    except UpstreamError as error:
        terminal_status = "failed"
        failure_code = error.code
        finish_reason = "length"

    content = "".join(content_parts)
    # 취소는 별도 요청(POST .../cancel)이 DB에 이미 cancel_requested로 남겨 뒀을 수
    # 있다 — finish_generation이 SQL CASE로 그 경우를 우선해 cancelled로 확정하고
    # 실제 저장값을 돌려준다. 여기서 계산한 terminal_status/failure_code는 그
    # 기본값일 뿐이고, 최종 판단은 항상 finish_generation의 반환값을 따른다(단일
    # 진실 소스 — 파이썬 쪽에서 다시 조회해 비교하지 않는다).
    persisted = chat_store.finish_generation(
        generation.id, status=terminal_status, content=content, failure_code=failure_code
    )
    metrics.GENERATIONS_FINISHED.labels(
        mode=generation.mode, terminal_reason=persisted.status
    ).inc()
    metrics.TOTAL_GENERATION_SECONDS.observe(time.monotonic() - start)
    yield _terminal_event(persisted, fallback_finish_reason=finish_reason)


def _terminal_event(persisted: Generation, *, fallback_finish_reason: str) -> str:
    """저장된 실제 status를 기준으로 done/error SSE 프레임을 만든다."""
    if persisted.status == "completed":
        return format_done(generation_id=persisted.id, finish_reason=fallback_finish_reason)
    status_field = "cancelled" if persisted.status == "cancelled" else "failed"
    return format_error(
        generation_id=persisted.id,
        code=persisted.failure_code or "cancelled",
        message=(
            "응답 생성이 중단됐습니다."
            if persisted.status == "cancelled"
            else "응답 생성에 실패했습니다."
        ),
        status=status_field,
    )
