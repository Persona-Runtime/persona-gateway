"""채팅(conversations/user_messages/generations)의 저수준 Postgres 접근.

`repository.py`(PostgresPersonaStore)와 같은 패턴을 그대로 따른다 — raw psycopg3,
`with pool.connection(): with connection.transaction(): with cursor(row_factory=
dict_row)`, 예외로 도메인 규칙 위반을 알린다. `PersonaStore`를 감싸지 않고
`ConnectionPool`을 직접 받는다(retrieval/search.py와 같은 이유 — 저수준 모듈).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ..repository import NotIndexed, PersonaNotFound, SchemaNotReady, chat_schema_ready
from ..retrieval.prompt import Message

# 활성 상태 — 이 상태들이면 "사용자당 활성 generation 1개" 슬롯을 쥐고 있다.
ACTIVE_STATUSES = ("queued", "running", "cancel_requested", "reconciling")
TERMINAL_STATUSES = ("completed", "cancelled", "failed")

# reconciling→failed 지연 해소 기준. background sweep은 없다 — 같은 사용자의 다음
# generation 요청이 잠금 안에서 들어왔을 때만, 이 시간을 넘겼으면 그 자리에서
# 해소한다(feedback.md 확정 정책).
RECONCILIATION_TIMEOUT_SECONDS = 300

CHAT_COMPLETION_OPERATION = "chat_completion"
CREATE_CONVERSATION_OPERATION = "create_conversation"
RETRY_GENERATION_OPERATION = "retry_generation"


class ConversationNotFound(Exception):
    """소유자가 다르거나 없는 대화. 캐릭터와 같은 이유로 둘을 구분해 알리지 않는다."""


class GenerationNotFound(Exception):
    pass


class GenerationInProgress(Exception):
    """이 사용자(캐릭터 무관)가 이미 활성 generation을 갖고 있다. 계약 §7의
    409 `generation_in_progress` 그대로 쓴다."""


class IdempotencyConflict(Exception):
    """같은 키에 다른 요청 본문이 왔다."""


class RetryNotAllowed(Exception):
    """재시도를 거절한다. code는 계약 §7이 정한 값을 그대로 쓴다:
    `retry_not_latest`(대화의 최신 질문·최신 시도가 아님),
    `retry_input_unavailable`(원본이 검색 단계에 이르기 전에 실패해 재사용할
    input_snapshot이 없음), `generation_in_progress`(원본 자신이 아직 활성 상태)."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Citation:
    id: UUID
    source_id: UUID
    version_id: UUID
    title: str
    excerpt: str

    def to_json(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "source_id": str(self.source_id),
            "version_id": str(self.version_id),
            "title": self.title,
            "excerpt": self.excerpt,
        }

    @staticmethod
    def from_json(value: dict[str, object]) -> "Citation":
        return Citation(
            id=UUID(value["id"]),  # type: ignore[arg-type]
            source_id=UUID(value["source_id"]),  # type: ignore[arg-type]
            version_id=UUID(value["version_id"]),  # type: ignore[arg-type]
            title=value["title"],  # type: ignore[arg-type]
            excerpt=value["excerpt"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class Conversation:
    id: UUID
    persona_id: UUID
    initial_version_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime
    active_generation_id: UUID | None
    material_changed: bool


@dataclass(frozen=True)
class UserMessage:
    id: UUID
    conversation_id: UUID
    content: str
    created_at: datetime


@dataclass(frozen=True)
class InputSnapshot:
    """retry가 검색을 다시 돌리지 않고 그대로 재사용하는 immutable 입력.

    `messages`(build_messages()가 만든 최종 프롬프트 그대로)까지 얼려 둔다 —
    citations·question만 있으면 retry 때 build_messages를 다시 불러야 하는데, 그때는
    원본 RetrievedChunk(내용 전체)가 없어 재구성할 수 없다. 프롬프트 자체를 얼리면
    재구성 문제 자체가 없어진다.
    """

    question: str
    citations: list[Citation]
    version_id: UUID
    messages: list[Message]

    def to_json(self) -> dict[str, object]:
        return {
            "question": self.question,
            "citations": [c.to_json() for c in self.citations],
            "version_id": str(self.version_id),
            "messages": [{"role": m.role, "content": m.content} for m in self.messages],
        }

    @staticmethod
    def from_json(value: dict[str, object]) -> "InputSnapshot":
        return InputSnapshot(
            question=value["question"],  # type: ignore[arg-type]
            citations=[Citation.from_json(c) for c in value["citations"]],  # type: ignore[union-attr]
            version_id=UUID(value["version_id"]),  # type: ignore[arg-type]
            messages=[Message(role=m["role"], content=m["content"]) for m in value["messages"]],  # type: ignore[union-attr,index]
        )


@dataclass(frozen=True)
class Generation:
    id: UUID
    conversation_id: UUID
    user_message_id: UUID
    version_id: UUID
    mode: str
    status: str
    content: str
    citations: list[Citation]
    failure_code: str | None
    retry_of_generation_id: UUID | None
    input_snapshot: InputSnapshot | None
    created_at: datetime
    finished_at: datetime | None

    @property
    def assistant_message_id(self) -> UUID:
        # 별도 컬럼이 없다 — 이 행 자체가 assistant 메시지 자리다.
        return self.id

    @property
    def can_retry(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class MessageTurn:
    user_message: UserMessage
    generations: list[Generation]


def _generation(row: dict[str, object]) -> Generation:
    citations_json = row["citations"]
    input_snapshot_json = row["input_snapshot"]
    return Generation(
        id=row["id"],  # type: ignore[arg-type]
        conversation_id=row["conversation_id"],  # type: ignore[arg-type]
        user_message_id=row["user_message_id"],  # type: ignore[arg-type]
        version_id=row["version_id"],  # type: ignore[arg-type]
        mode=row["mode"],  # type: ignore[arg-type]
        status=row["status"],  # type: ignore[arg-type]
        content=row["content"],  # type: ignore[arg-type]
        citations=[Citation.from_json(c) for c in citations_json],  # type: ignore[union-attr]
        failure_code=row["failure_code"],  # type: ignore[arg-type]
        retry_of_generation_id=row["retry_of_generation_id"],  # type: ignore[arg-type]
        input_snapshot=(
            InputSnapshot.from_json(input_snapshot_json)  # type: ignore[arg-type]
            if input_snapshot_json is not None
            else None
        ),
        created_at=row["created_at"],  # type: ignore[arg-type]
        finished_at=row["finished_at"],  # type: ignore[arg-type]
    )


def fingerprint_conversation_create(persona_id: UUID) -> bytes:
    return hashlib.sha256(str(persona_id).encode("utf-8")).digest()


def fingerprint_chat_completion(conversation_id: UUID, message: str) -> bytes:
    joined = "\x00".join((str(conversation_id), message))
    return hashlib.sha256(joined.encode("utf-8")).digest()


def fingerprint_retry(generation_id: UUID) -> bytes:
    return hashlib.sha256(str(generation_id).encode("utf-8")).digest()


class ChatStore:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    # --- 대화 -----------------------------------------------------------

    def create_conversation(
        self, owner_subject: str, persona_id: UUID, idempotency_key: UUID
    ) -> Conversation:
        fingerprint = fingerprint_conversation_create(persona_id)
        scope = f"/v1/personas/{persona_id}/conversations"
        with self.pool.connection() as connection, connection.transaction():
            with connection.cursor(row_factory=dict_row) as cur:
                if not chat_schema_ready(cur):
                    raise SchemaNotReady

                cur.execute(
                    """
                    SELECT request_fingerprint, result_id
                    FROM persona_minimal.chat_idempotency_records
                    WHERE owner_subject = %s AND operation = %s AND target_scope = %s
                      AND idempotency_key = %s
                    """,
                    (owner_subject, CREATE_CONVERSATION_OPERATION, scope, idempotency_key),
                )
                record = cur.fetchone()
                if record is not None:
                    if bytes(record["request_fingerprint"]) != fingerprint:
                        raise IdempotencyConflict
                    return self._conversation_by_id(cur, record["result_id"])

                # 소유권 확인 + 이름(대화 제목 기본값)을 함께 얻는다. FOR UPDATE로
                # 캐릭터 삭제와 경합하지 않게 한다(create_persona의 3개 한도 검사와
                # 같은 이유).
                cur.execute(
                    """
                    SELECT name FROM persona_minimal.personas
                    WHERE id = %s AND owner_subject = %s AND deleted_at IS NULL
                    FOR UPDATE
                    """,
                    (persona_id, owner_subject),
                )
                persona_row = cur.fetchone()
                if persona_row is None:
                    raise PersonaNotFound

                cur.execute(
                    """
                    SELECT version_id, indexed_revision FROM persona_minimal.material_versions
                    WHERE persona_id = %s
                    """,
                    (persona_id,),
                )
                version_row = cur.fetchone()
                if version_row is None or version_row["indexed_revision"] is None:
                    # "적용된 version" = 색인에 성공한 버전이 있음(service-api-v1.md
                    # §2 indexed_revision 정의). 별도 "적용" 기능은 아직 없다.
                    raise NotIndexed

                conversation_id = uuid4()
                cur.execute(
                    """
                    INSERT INTO persona_minimal.conversations(
                        id, persona_id, owner_subject, initial_version_id, title
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        conversation_id,
                        persona_id,
                        owner_subject,
                        version_row["version_id"],
                        persona_row["name"],
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO persona_minimal.chat_idempotency_records(
                        owner_subject, operation, target_scope, idempotency_key,
                        request_fingerprint, result_id
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        owner_subject,
                        CREATE_CONVERSATION_OPERATION,
                        scope,
                        idempotency_key,
                        fingerprint,
                        conversation_id,
                    ),
                )
                return self._conversation_by_id(cur, conversation_id)

    def _conversation_by_id(self, cur, conversation_id: UUID) -> Conversation:
        cur.execute(
            """
            SELECT c.id, c.persona_id, c.initial_version_id, c.title, c.created_at, c.updated_at,
                   mv.version_id AS current_version_id,
                   (SELECT g.id FROM persona_minimal.generations AS g
                    WHERE g.conversation_id = c.id AND g.status = ANY(%s)
                    ORDER BY g.created_at DESC LIMIT 1) AS active_generation_id
            FROM persona_minimal.conversations AS c
            LEFT JOIN persona_minimal.material_versions AS mv ON mv.persona_id = c.persona_id
            WHERE c.id = %s
            """,
            (list(ACTIVE_STATUSES), conversation_id),
        )
        row = cur.fetchone()
        return self._conversation_row(row)

    @staticmethod
    def _conversation_row(row: dict[str, object]) -> Conversation:
        return Conversation(
            id=row["id"],  # type: ignore[arg-type]
            persona_id=row["persona_id"],  # type: ignore[arg-type]
            initial_version_id=row["initial_version_id"],  # type: ignore[arg-type]
            title=row["title"],  # type: ignore[arg-type]
            created_at=row["created_at"],  # type: ignore[arg-type]
            updated_at=row["updated_at"],  # type: ignore[arg-type]
            active_generation_id=row["active_generation_id"],  # type: ignore[arg-type]
            material_changed=row["current_version_id"] != row["initial_version_id"],
        )

    def list_conversations(
        self, owner_subject: str, persona_id: UUID, limit: int, cursor: tuple[datetime, UUID] | None
    ) -> list[Conversation]:
        values: list[object] = [list(ACTIVE_STATUSES), persona_id, owner_subject]
        query = """
            SELECT c.id, c.persona_id, c.initial_version_id, c.title, c.created_at, c.updated_at,
                   mv.version_id AS current_version_id,
                   (SELECT g.id FROM persona_minimal.generations AS g
                    WHERE g.conversation_id = c.id AND g.status = ANY(%s)
                    ORDER BY g.created_at DESC LIMIT 1) AS active_generation_id
            FROM persona_minimal.conversations AS c
            LEFT JOIN persona_minimal.material_versions AS mv ON mv.persona_id = c.persona_id
            WHERE c.persona_id = %s AND c.owner_subject = %s
        """
        if cursor is not None:
            query += " AND (c.created_at, c.id) < (%s, %s)"
            values.extend([cursor[0], cursor[1]])
        query += " ORDER BY c.created_at DESC, c.id DESC LIMIT %s"
        values.append(limit)
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cur:
                if not chat_schema_ready(cur):
                    raise SchemaNotReady
                cur.execute(query, values)
                return [self._conversation_row(row) for row in cur.fetchall()]

    def list_messages(
        self,
        owner_subject: str,
        conversation_id: UUID,
        limit: int,
        cursor: tuple[datetime, UUID] | None,
    ) -> list[MessageTurn]:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cur:
                if not chat_schema_ready(cur):
                    raise SchemaNotReady
                cur.execute(
                    "SELECT 1 FROM persona_minimal.conversations WHERE id = %s AND owner_subject = %s",
                    (conversation_id, owner_subject),
                )
                if cur.fetchone() is None:
                    raise ConversationNotFound

                values: list[object] = [conversation_id]
                query = """
                    SELECT id, conversation_id, content, created_at
                    FROM persona_minimal.user_messages
                    WHERE conversation_id = %s
                """
                if cursor is not None:
                    query += " AND (created_at, id) > (%s, %s)"
                    values.extend([cursor[0], cursor[1]])
                query += " ORDER BY created_at, id LIMIT %s"
                values.append(limit)
                cur.execute(query, values)
                message_rows = cur.fetchall()
                if not message_rows:
                    return []

                message_ids = [row["id"] for row in message_rows]
                cur.execute(
                    """
                    SELECT id, conversation_id, user_message_id, version_id, mode, status, content,
                           citations, failure_code, retry_of_generation_id, input_snapshot,
                           created_at, finished_at
                    FROM persona_minimal.generations
                    WHERE user_message_id = ANY(%s)
                    ORDER BY created_at
                    """,
                    (message_ids,),
                )
                generations_by_message: dict[UUID, list[Generation]] = {}
                for row in cur.fetchall():
                    generations_by_message.setdefault(row["user_message_id"], []).append(
                        _generation(row)
                    )

                return [
                    MessageTurn(
                        user_message=UserMessage(
                            id=row["id"],
                            conversation_id=row["conversation_id"],
                            content=row["content"],
                            created_at=row["created_at"],
                        ),
                        generations=generations_by_message.get(row["id"], []),
                    )
                    for row in message_rows
                ]

    # 프롬프트 이력 조회가 되짚어 볼 최대 턴 수 — build_messages가 글자 수 예산으로
    # 다시 자르므로 이 값은 "그보다 훨씬 넉넉한 안전 상한"일 뿐이다.
    HISTORY_LOOKBACK_TURNS = 20

    def load_history(self, conversation_id: UUID, *, before_user_message_id: UUID) -> list[Message]:
        """이 대화에서 `before_user_message_id`보다 먼저 온, **completed**
        generation이 있는 턴만 오래된순으로 돌려준다. 실패·취소·진행 중 답변은
        기본 제외한다(feedback.md: "중단·실패 답변은 다음 모델 문맥에서 기본 제외")."""
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT question, answer FROM (
                        SELECT DISTINCT ON (um.id)
                               um.id AS message_id, um.created_at AS message_created_at,
                               um.content AS question, g.content AS answer
                        FROM persona_minimal.user_messages AS um
                        JOIN persona_minimal.generations AS g ON g.user_message_id = um.id
                        WHERE um.conversation_id = %s
                          AND (um.created_at, um.id) < (
                              SELECT created_at, id FROM persona_minimal.user_messages WHERE id = %s
                          )
                          AND g.status = 'completed'
                        ORDER BY um.id, g.created_at DESC
                    ) AS latest_per_message
                    ORDER BY message_created_at DESC
                    LIMIT %s
                    """,
                    (conversation_id, before_user_message_id, self.HISTORY_LOOKBACK_TURNS),
                )
                rows = list(reversed(cur.fetchall()))
        messages: list[Message] = []
        for row in rows:
            messages.append(Message(role="user", content=row["question"]))
            messages.append(Message(role="assistant", content=row["answer"]))
        return messages

    # --- generation 시작(accept 트랜잭션) --------------------------------

    def create_generation_queued(
        self, owner_subject: str, conversation_id: UUID, message: str, idempotency_key: UUID
    ) -> tuple[Generation, bool]:
        """feedback.md 처리 순서 1~5단계. 검색(6단계)은 여기 없다 — 네트워크 호출이라
        이 트랜잭션 밖에서 한다. 반환값의 bool은 idempotent replay 여부다."""
        fingerprint = fingerprint_chat_completion(conversation_id, message)
        scope = f"/v1/conversations/{conversation_id}/completions"
        with self.pool.connection() as connection, connection.transaction():
            with connection.cursor(row_factory=dict_row) as cur:
                if not chat_schema_ready(cur):
                    raise SchemaNotReady

                # 사용자 행을 잠가 "활성 generation 1개" 확인·삽입을 직렬화한다
                # (create_persona의 3개 한도 검사와 같은 패턴 — advisory lock이
                # 아니라 이 트랜잭션 동안만 필요한 행 잠금으로 충분하다).
                cur.execute(
                    "SELECT subject FROM persona_minimal.users WHERE subject = %s FOR UPDATE",
                    (owner_subject,),
                )
                if cur.fetchone() is None:
                    raise ConversationNotFound

                cur.execute(
                    """
                    SELECT request_fingerprint, result_id
                    FROM persona_minimal.chat_idempotency_records
                    WHERE owner_subject = %s AND operation = %s AND target_scope = %s
                      AND idempotency_key = %s
                    """,
                    (owner_subject, CHAT_COMPLETION_OPERATION, scope, idempotency_key),
                )
                record = cur.fetchone()
                if record is not None:
                    if bytes(record["request_fingerprint"]) != fingerprint:
                        raise IdempotencyConflict
                    return self._generation_by_id(cur, record["result_id"]), True

                cur.execute(
                    """
                    SELECT c.persona_id FROM persona_minimal.conversations AS c
                    JOIN persona_minimal.personas AS p ON p.id = c.persona_id
                    WHERE c.id = %s AND c.owner_subject = %s AND p.deleted_at IS NULL
                    """,
                    (conversation_id, owner_subject),
                )
                conversation_row = cur.fetchone()
                if conversation_row is None:
                    # 대화 자체가 없거나, 있어도 캐릭터가 삭제 중이면 같은 404로
                    # 묶는다(캐릭터 소유권 위반과 부재를 구분하지 않는 기존 관례와
                    # 같은 이유 — 삭제 중인 캐릭터의 대화가 여전히 존재한다는 것도
                    # 새어 나가지 않게 한다).
                    raise ConversationNotFound
                persona_id = conversation_row["persona_id"]

                self._reject_or_resolve_active_generation(cur, owner_subject)

                cur.execute(
                    "SELECT version_id, indexed_revision FROM persona_minimal.material_versions "
                    "WHERE persona_id = %s",
                    (persona_id,),
                )
                version_row = cur.fetchone()
                if version_row is None or version_row["indexed_revision"] is None:
                    raise NotIndexed

                user_message_id = uuid4()
                cur.execute(
                    """
                    INSERT INTO persona_minimal.user_messages(id, conversation_id, content)
                    VALUES (%s, %s, %s)
                    """,
                    (user_message_id, conversation_id, message),
                )
                generation_id = uuid4()
                cur.execute(
                    """
                    INSERT INTO persona_minimal.generations(
                        id, conversation_id, user_message_id, version_id, mode, status
                    ) VALUES (%s, %s, %s, %s, 'mock', 'queued')
                    """,
                    (generation_id, conversation_id, user_message_id, version_row["version_id"]),
                )
                cur.execute(
                    """
                    INSERT INTO persona_minimal.chat_idempotency_records(
                        owner_subject, operation, target_scope, idempotency_key,
                        request_fingerprint, result_id
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        owner_subject,
                        CHAT_COMPLETION_OPERATION,
                        scope,
                        idempotency_key,
                        fingerprint,
                        generation_id,
                    ),
                )
                cur.execute(
                    "UPDATE persona_minimal.conversations SET updated_at = now() WHERE id = %s",
                    (conversation_id,),
                )
                return self._generation_by_id(cur, generation_id), False

    def _reject_or_resolve_active_generation(self, cur, owner_subject: str) -> None:
        """활성 generation이 있으면 원칙적으로 GenerationInProgress를 던진다.

        단, 그 활성 generation이 reconciling이고 heartbeat_at이
        RECONCILIATION_TIMEOUT_SECONDS보다 오래됐으면 — 재시작·연결 끊김 뒤 아무도
        해소하지 않은 채 방치된 것으로 보고 이 자리에서 failed(reconciliation_timeout)로
        닫고 슬롯을 연다(feedback.md 확정 정책 — 별도 background sweep 없음, "같은
        사용자의 다음 요청이 잠금 안에서 들어왔을 때만" 해소).
        """
        cur.execute(
            """
            SELECT g.id, g.status, g.heartbeat_at
            FROM persona_minimal.generations AS g
            JOIN persona_minimal.conversations AS c ON c.id = g.conversation_id
            WHERE c.owner_subject = %s AND g.status = ANY(%s)
            ORDER BY g.created_at DESC
            LIMIT 1
            """,
            (owner_subject, list(ACTIVE_STATUSES)),
        )
        row = cur.fetchone()
        if row is None:
            return
        if row["status"] == "reconciling":
            age = datetime.now(timezone.utc) - row["heartbeat_at"]
            if age > timedelta(seconds=RECONCILIATION_TIMEOUT_SECONDS):
                cur.execute(
                    """
                    UPDATE persona_minimal.generations
                    SET status = 'failed', failure_code = 'reconciliation_timeout', finished_at = now()
                    WHERE id = %s
                    """,
                    (row["id"],),
                )
                return
        raise GenerationInProgress

    def _generation_by_id(self, cur, generation_id: UUID) -> Generation:
        cur.execute(
            """
            SELECT id, conversation_id, user_message_id, version_id, mode, status, content,
                   citations, failure_code, retry_of_generation_id, input_snapshot,
                   created_at, finished_at
            FROM persona_minimal.generations WHERE id = %s
            """,
            (generation_id,),
        )
        return _generation(cur.fetchone())

    def get_generation(self, owner_subject: str, generation_id: UUID) -> Generation:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cur:
                if not chat_schema_ready(cur):
                    raise SchemaNotReady
                cur.execute(
                    """
                    SELECT g.id, g.conversation_id, g.user_message_id, g.version_id, g.mode,
                           g.status, g.content, g.citations, g.failure_code,
                           g.retry_of_generation_id, g.input_snapshot, g.created_at, g.finished_at
                    FROM persona_minimal.generations AS g
                    JOIN persona_minimal.conversations AS c ON c.id = g.conversation_id
                    WHERE g.id = %s AND c.owner_subject = %s
                    """,
                    (generation_id, owner_subject),
                )
                row = cur.fetchone()
                if row is None:
                    raise GenerationNotFound
                return _generation(row)

    # --- generation 진행 --------------------------------------------------

    def mark_generation_running(self, generation_id: UUID, snapshot: InputSnapshot) -> None:
        with self.pool.connection() as connection, connection.transaction():
            connection.execute(
                """
                UPDATE persona_minimal.generations
                SET status = 'running', citations = %s, input_snapshot = %s, heartbeat_at = now()
                WHERE id = %s
                """,
                (
                    json.dumps([c.to_json() for c in snapshot.citations]),
                    json.dumps(snapshot.to_json()),
                    generation_id,
                ),
            )

    def finish_generation(
        self, generation_id: UUID, *, status: str, content: str, failure_code: str | None
    ) -> None:
        """계약대로 done/error SSE를 보내기 **전에** 호출해 DB에 먼저 반영한다."""
        with self.pool.connection() as connection, connection.transaction():
            connection.execute(
                """
                UPDATE persona_minimal.generations
                SET status = %s, content = %s, failure_code = %s, finished_at = now()
                WHERE id = %s
                """,
                (status, content, failure_code, generation_id),
            )

    def mark_generation_reconciling(self, generation_id: UUID) -> None:
        """연결이 끊기거나 Gateway가 재시작될 때 호출한다 — 성공도 실패도 아니라고
        정직하게 표시한다. "프로세스가 죽었으니 failed로 슬롯 해제"는 하지 않는다."""
        with self.pool.connection() as connection, connection.transaction():
            connection.execute(
                """
                UPDATE persona_minimal.generations
                SET status = 'reconciling', heartbeat_at = now()
                WHERE id = %s AND status IN ('queued', 'running', 'cancel_requested')
                """,
                (generation_id,),
            )

    def reconcile_stale_generations_on_startup(self) -> int:
        """Gateway 기동 시 이전 프로세스가 남긴 running/cancel_requested/queued
        generation을 reconciling으로 전환한다(material_versions의 stale processing
        정리와 같은 lifespan 패턴). 반환값은 전환된 행 수(로그용)."""
        with self.pool.connection() as connection, connection.transaction():
            cur = connection.execute(
                """
                UPDATE persona_minimal.generations
                SET status = 'reconciling', heartbeat_at = now()
                WHERE status IN ('queued', 'running', 'cancel_requested')
                """
            )
            return cur.rowcount

    def request_cancel(self, owner_subject: str, generation_id: UUID) -> Generation:
        with self.pool.connection() as connection, connection.transaction():
            with connection.cursor(row_factory=dict_row) as cur:
                if not chat_schema_ready(cur):
                    raise SchemaNotReady
                cur.execute(
                    """
                    SELECT g.id FROM persona_minimal.generations AS g
                    JOIN persona_minimal.conversations AS c ON c.id = g.conversation_id
                    WHERE g.id = %s AND c.owner_subject = %s
                    FOR UPDATE OF g
                    """,
                    (generation_id, owner_subject),
                )
                if cur.fetchone() is None:
                    raise GenerationNotFound
                # terminal이면 그대로 둔다(계약: "200은 취소 완료 보장이 아님... terminal
                # 상태면 그대로 반환"). queued/running만 cancel_requested로 바꾼다 —
                # reconciling은 "결과를 모른다"는 뜻이라 cancel_requested로 덮으면
                # 오히려 아는 척하는 것이 된다.
                cur.execute(
                    """
                    UPDATE persona_minimal.generations SET status = 'cancel_requested'
                    WHERE id = %s AND status IN ('queued', 'running')
                    """,
                    (generation_id,),
                )
                return self._generation_by_id(cur, generation_id)

    def create_retry_generation(
        self, owner_subject: str, generation_id: UUID, idempotency_key: UUID
    ) -> tuple[Generation, bool]:
        fingerprint = fingerprint_retry(generation_id)
        scope = f"/v1/generations/{generation_id}/retry"
        with self.pool.connection() as connection, connection.transaction():
            with connection.cursor(row_factory=dict_row) as cur:
                if not chat_schema_ready(cur):
                    raise SchemaNotReady

                cur.execute(
                    "SELECT subject FROM persona_minimal.users WHERE subject = %s FOR UPDATE",
                    (owner_subject,),
                )
                if cur.fetchone() is None:
                    raise GenerationNotFound

                cur.execute(
                    """
                    SELECT request_fingerprint, result_id
                    FROM persona_minimal.chat_idempotency_records
                    WHERE owner_subject = %s AND operation = %s AND target_scope = %s
                      AND idempotency_key = %s
                    """,
                    (owner_subject, RETRY_GENERATION_OPERATION, scope, idempotency_key),
                )
                record = cur.fetchone()
                if record is not None:
                    if bytes(record["request_fingerprint"]) != fingerprint:
                        raise IdempotencyConflict
                    return self._generation_by_id(cur, record["result_id"]), True

                cur.execute(
                    """
                    SELECT g.id, g.conversation_id, g.user_message_id, g.version_id, g.status,
                           g.input_snapshot
                    FROM persona_minimal.generations AS g
                    JOIN persona_minimal.conversations AS c ON c.id = g.conversation_id
                    JOIN persona_minimal.personas AS p ON p.id = c.persona_id
                    WHERE g.id = %s AND c.owner_subject = %s AND p.deleted_at IS NULL
                    """,
                    (generation_id, owner_subject),
                )
                original = cur.fetchone()
                if original is None:
                    raise GenerationNotFound
                if original["status"] not in TERMINAL_STATUSES:
                    raise RetryNotAllowed("generation_in_progress")
                if original["input_snapshot"] is None:
                    # 검색 단계에 이르기 전에 실패한 generation — 재사용할 immutable
                    # 입력이 없다. 여기서 새로 검색을 돌리지 않는다(계약이 "원래 입력"을
                    # 요구한다).
                    raise RetryNotAllowed("retry_input_unavailable")

                cur.execute(
                    "SELECT id FROM persona_minimal.user_messages WHERE conversation_id = %s "
                    "ORDER BY created_at DESC, id DESC LIMIT 1",
                    (original["conversation_id"],),
                )
                latest_message = cur.fetchone()
                if latest_message is None or latest_message["id"] != original["user_message_id"]:
                    raise RetryNotAllowed("retry_not_latest")  # 최신 질문이 아니다

                cur.execute(
                    "SELECT id FROM persona_minimal.generations WHERE user_message_id = %s "
                    "ORDER BY created_at DESC, id DESC LIMIT 1",
                    (original["user_message_id"],),
                )
                latest_attempt = cur.fetchone()
                if latest_attempt is None or latest_attempt["id"] != original["id"]:
                    raise RetryNotAllowed("retry_not_latest")  # 최신 시도가 아니다

                self._reject_or_resolve_active_generation(cur, owner_subject)

                # 원본의 input_snapshot(질문·citations·version)을 그대로 복사해 검색을
                # 다시 돌리지 않으므로, queued가 아니라 바로 running으로 만든다 — 정상
                # 경로(create_generation_queued→mark_generation_running)의 queued는
                # "아직 검색 전"을 뜻하는데, retry는 애초에 검색할 게 없다.
                new_generation_id = uuid4()
                cur.execute(
                    """
                    INSERT INTO persona_minimal.generations(
                        id, conversation_id, user_message_id, version_id, mode, status,
                        retry_of_generation_id, input_snapshot, citations
                    ) VALUES (%s, %s, %s, %s, 'mock', 'running', %s, %s, %s)
                    """,
                    (
                        new_generation_id,
                        original["conversation_id"],
                        original["user_message_id"],
                        original["version_id"],
                        original["id"],
                        json.dumps(original["input_snapshot"]),
                        json.dumps(original["input_snapshot"]["citations"]),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO persona_minimal.chat_idempotency_records(
                        owner_subject, operation, target_scope, idempotency_key,
                        request_fingerprint, result_id
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        owner_subject,
                        RETRY_GENERATION_OPERATION,
                        scope,
                        idempotency_key,
                        fingerprint,
                        new_generation_id,
                    ),
                )
                return self._generation_by_id(cur, new_generation_id), False
