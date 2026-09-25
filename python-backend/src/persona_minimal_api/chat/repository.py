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

from ..repository import (
    NoActiveVersion,
    PersonaNotFound,
    SchemaNotReady,
    chat_schema_ready,
    lease_schema_ready,
)
from ..retrieval.prompt import Message
from . import metrics

# 활성 상태 — 이 상태들이면 "사용자당 활성 generation 1개" 슬롯을 쥐고 있다.
ACTIVE_STATUSES = ("queued", "running", "cancel_requested", "reconciling")
TERMINAL_STATUSES = ("completed", "cancelled", "failed")
# finish_generation/mark_generation_reconciling이 "아직 이 프로세스가 붙잡고 있는
# 살아있는 generation"으로 인정해 손댈 수 있는 상태. reconciling은 빠진다 —
# reconciling으로 전환됐다는 건 이미 이 코드 경로(제너레이터)가 GeneratorExit로
# 빠져나갔다는 뜻이라, 같은 제너레이터가 그 뒤 다시 finish_generation을 부를 일이
# 없다(만약 부른다면 그건 다른 프로세스/요청이 잘못 끼어든 것이므로 오히려 막아야
# 한다).
FINISHABLE_STATUSES = ("queued", "running", "cancel_requested")

# reconciling→failed 지연 해소 기준. background sweep은 없다 — 같은 사용자의 다음
# generation 요청이 잠금 안에서 들어왔을 때만, 이 시간을 넘겼으면 그 자리에서
# 해소한다(feedback.md 확정 정책).
RECONCILIATION_TIMEOUT_SECONDS = 300

# --- generation 소유권 lease (G-1, api/generation-ownership-lease-design.md) ---------
# 활성 generation은 그것을 스트리밍하는 Gateway 인스턴스(owner_instance_id)와, 그 인스턴스가
# 살아 있다고 볼 수 있는 기한(lease_expires_at)을 가진다. 살아 있는 인스턴스는 주기적으로
# 기한을 늘리고(chat/lease.py), 다른 인스턴스는 **기한이 지난 행만** 회수한다. 모든 시각
# 비교는 DB now()로 한다 — Pod마다 다른 시계로 만료를 판정하지 않는다.
#
# 기본 lease 길이(초). 실제 값은 설정(PERSONA_GENERATION_LEASE_SECONDS)이 정한다.
DEFAULT_LEASE_SECONDS = 30.0
# owner_instance_id가 없는 활성 행(0005 이전·호환 릴리스 구버전 Gateway가 만든 행)의 회수
# 유예. 소유자를 모르므로 "그 소유자가 아직 스트리밍 중일 수 있는 최대 시간"이 지난 뒤에만
# 회수한다 — 생성 전체 한도 180초(chat/service.py)에 여유 60초를 더했다. 이 값보다 짧으면
# 롤링 중 새 Pod가 살아 있는 구버전 Pod의 스트림을 끊는다(G-1 이전과 같은 문제).
LEGACY_OWNERLESS_GRACE_SECONDS = 240

# 회수 조건(SQL 조각). "활성이고, 소유자가 있으면 lease 만료, 없으면 유예 경과". 회수 UPDATE의
# WHERE에 그대로 넣어 판정과 전환을 한 문장으로 한다 — 두 인스턴스가 동시에 회수해도
# Postgres가 행 잠금 뒤 WHERE를 다시 평가하므로 같은 행은 한쪽만 바꾼다.
# 회수 시 남기는 heartbeat_at. reconciling의 300초 지연 해소는 "마지막으로 살아 있음이
# 확인된 시각"부터 센다(계약 §7). 회수한 시각(now())을 쓰면 이미 오래 방치된 행의 시계가
# 처음부터 다시 돌아 사용자가 300초를 또 기다린다. 그래서 소유자가 있으면 마지막 lease 기한
# (소유자가 살아 있다고 볼 수 있던 마지막 시각), 소유자 없는 구버전 행이면 기존 heartbeat_at을
# 그대로 남긴다. (정상 종료 반납·연결 종료는 소유자가 방금까지 살아 있었으므로 now()를 쓴다.)
_RECLAIM_SET = """
    status = 'reconciling',
    heartbeat_at = CASE WHEN owner_instance_id IS NOT NULL THEN lease_expires_at
                        ELSE heartbeat_at END
"""

_RECLAIMABLE_CONDITION = """
    status IN ('queued', 'running', 'cancel_requested')
    AND (
        (owner_instance_id IS NOT NULL AND lease_expires_at < now())
        OR (owner_instance_id IS NULL
            AND heartbeat_at < now() - make_interval(secs => %(legacy_grace)s))
    )
"""

# bridge 릴리스가 0004 DB(lease 컬럼 없음)에서 썼던 회수 규칙. 모든 행이 소유자를 모르므로
# "소유자 없는 행" 규칙 하나만 남는다 — 전역 reconcile은 하지 않는다. 살아 있는 스트림은 생성
# 전체 한도(180초) 안에 끝나므로 240초 유예에 걸리지 않는다. 컬럼을 참조하지 않아야 0004에서
# 실행된다. bridge 창을 닫은 뒤(0005만 허용)에는 0004 DB에서 readyz가 503이라, 트래픽을 받지
# 않는 Pod의 기동 회수에서만 닿는다 — 다음 스키마 전환에서 재사용할 호환 창 도구로 남긴다.
_RECLAIM_SET_WITHOUT_LEASE = "status = 'reconciling'"
_RECLAIMABLE_CONDITION_WITHOUT_LEASE = """
    status IN ('queued', 'running', 'cancel_requested')
    AND heartbeat_at < now() - make_interval(secs => %(legacy_grace)s)
"""


def _reclaim_sql(lease_ready: bool) -> tuple[str, str]:
    """(SET 절, WHERE 조건)을 lease 스키마 유무에 맞게 고른다. 두 회수 경로가 함께 쓴다."""
    if lease_ready:
        return _RECLAIM_SET, _RECLAIMABLE_CONDITION
    return _RECLAIM_SET_WITHOUT_LEASE, _RECLAIMABLE_CONDITION_WITHOUT_LEASE


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
    def __init__(
        self,
        pool: ConnectionPool,
        *,
        mode: str = "mock",
        instance_id: UUID | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ):
        """mode는 이 프로세스가 어떤 업스트림으로 답하는지다(`PERSONA_CHAT_INFERENCE_MODE`).

        generation 행에 그대로 적혀 SSE meta.mode와 Prometheus mode 라벨이 된다. 기본값이
        mock인 이유는 테스트가 pool만 넘겨 만들기 때문이고, 실제 앱은 main.create_app이
        설정값을 넘긴다. DB CHECK가 'mock'·'llm'만 허용하므로 다른 값은 INSERT에서 걸린다.
        """
        self.pool = pool
        self.mode = mode
        # 이 프로세스(앱 인스턴스)의 소유자 ID. 기동마다 새로 만든다 — Pod 이름을 쓰지 않는
        # 이유는 같은 이름의 컨테이너가 재시작돼도 이전 프로세스와 구분해야 하기 때문이다.
        # 로그·메트릭 label에는 남기지 않는다.
        self.instance_id = instance_id or uuid4()
        self.lease_seconds = lease_seconds
        # lease 컬럼(0005)이 있음을 한 번 확인하면 True로 고정한다. False인 동안은 매번 다시 본다 —
        # bridge 릴리스가 떠 있는 중에 migration이 적용되면 재시작 없이 다음 요청부터 lease를
        # 쓰게 하려는 것이었다(0005 전용인 지금도 NotReady Pod가 0005 적용 뒤 바로 lease를 쓴다). 운영 중 downgrade는 정책상 하지 않으므로 True를 되돌리지 않는다.
        self._lease_schema_confirmed = False

    # --- lease 스키마 인식(bridge) ----------------------------------------

    def _lease_schema_available(self, connection) -> bool:
        if self._lease_schema_confirmed:
            return True
        with connection.cursor(row_factory=dict_row) as cur:
            self._lease_schema_confirmed = lease_schema_ready(cur)
        return self._lease_schema_confirmed

    def _stamp_owner(self, connection, generation_id: UUID) -> None:
        """이 인스턴스를 소유자로, lease를 지금부터 lease 길이까지로 기록한다(0005에서만).

        호출자의 트랜잭션 안에서 부른다 — INSERT·running 전이와 같은 트랜잭션으로 커밋되므로
        소유권 기록은 그 전이와 원자적이다. 0004(bridge 기간)에서는 아무것도 하지 않는다.
        """
        if not self._lease_schema_available(connection):
            return
        connection.execute(
            """
            UPDATE persona_minimal.generations
            SET owner_instance_id = %s, lease_expires_at = now() + make_interval(secs => %s)
            WHERE id = %s
            """,
            (self.instance_id, self.lease_seconds, generation_id),
        )

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
                    SELECT name, active_version_id FROM persona_minimal.personas
                    WHERE id = %s AND owner_subject = %s AND deleted_at IS NULL
                    FOR UPDATE
                    """,
                    (persona_id, owner_subject),
                )
                persona_row = cur.fetchone()
                if persona_row is None:
                    raise PersonaNotFound

                # 계약 §7: "새 대화는 적용본이 있어야 한다." 색인만 끝난 초안으로는
                # 시작하지 않는다 — 색인은 검색 재료가 준비됐다는 뜻일 뿐이고, 그것을
                # 실제로 쓸지는 활성화(draft/activate)가 정한다.
                if persona_row["active_version_id"] is None:
                    raise NoActiveVersion

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
                        persona_row["active_version_id"],
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

    # material_changed는 "이 대화가 시작한 적용본이 아직도 그 캐릭터의 적용본인가"다.
    # personas.active_version_id를 직접 읽는다 — material_versions를 persona_id로
    # 조인하면 캐릭터에 version이 여럿(적용본 + 새 초안)일 때 대화가 그 수만큼
    # 중복되고, 고르는 행에 따라 판정도 달라진다. personas는 c.persona_id의 FK
    # 대상이라 반드시 한 행이다.
    _CONVERSATION_COLUMNS = """
        SELECT c.id, c.persona_id, c.initial_version_id, c.title, c.created_at, c.updated_at,
               p.active_version_id AS current_version_id,
               (SELECT g.id FROM persona_minimal.generations AS g
                WHERE g.conversation_id = c.id AND g.status = ANY(%s)
                ORDER BY g.created_at DESC LIMIT 1) AS active_generation_id
        FROM persona_minimal.conversations AS c
        JOIN persona_minimal.personas AS p ON p.id = c.persona_id
    """

    def _conversation_by_id(self, cur, conversation_id: UUID) -> Conversation:
        cur.execute(
            self._CONVERSATION_COLUMNS + " WHERE c.id = %s",
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
        query = self._CONVERSATION_COLUMNS + " WHERE c.persona_id = %s AND c.owner_subject = %s"
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
        # 순서가 계약이다: (1) replay·키 충돌을 먼저 답하고 (2) 새 요청일 때만 죽은 소유자의 슬롯을
        # 회수·커밋한 뒤 (3) 본 트랜잭션에서 잠금·idempotency 재확인·슬롯 검사·삽입을 한다.
        # 회수를 (1)보다 먼저 하면 같은 키 재전송이 응답 전에 다른 활성 행을 바꾼다 — "replay는
        # 한도 검사보다 먼저"를 어긴다. 회수를 (3) 안에 넣으면 슬롯 검사의 409가 트랜잭션을
        # ROLLBACK해 회수도 사라진다. (3)에서 idempotency를 다시 보는 이유: (1)과 (3) 사이에
        # 같은 키의 다른 요청이 먼저 삽입했을 수 있다 — 그 경우 replay로 답한다.
        replayed = self._replay_before_reclaim(
            owner_subject, CHAT_COMPLETION_OPERATION, scope, idempotency_key, fingerprint
        )
        if replayed is not None:
            return replayed, True
        self.reclaim_user_expired_generations(owner_subject)
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

                # 질문마다 **그 시점의 적용본**을 서버가 고른다(계약 §7). 대화의
                # initial_version_id를 쓰지 않는 이유: 대화 도중 새 적용본이 생기면
                # 이후 질문은 새 적용본으로 답해야 하고, 그 사실을 generation.version_id에
                # 남겨 어떤 적용본이 답했는지 나중에 확인할 수 있어야 한다.
                cur.execute(
                    "SELECT active_version_id FROM persona_minimal.personas WHERE id = %s",
                    (persona_id,),
                )
                version_row = cur.fetchone()
                if version_row is None or version_row["active_version_id"] is None:
                    raise NoActiveVersion

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
                    ) VALUES (%s, %s, %s, %s, %s, 'queued')
                    """,
                    (
                        generation_id,
                        conversation_id,
                        user_message_id,
                        version_row["active_version_id"],
                        self.mode,
                    ),
                )
                # 접수한 인스턴스가 곧 스트리밍할 인스턴스다 — queued부터 소유한다. 소유자 없이
                # 두면 검색 중인 행이 "구버전 행"으로 오인된다. 같은 트랜잭션이라 원자적이다.
                self._stamp_owner(connection, generation_id)
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

        단, 그 활성 generation이 reconciling **또는 cancel_requested**이고
        heartbeat_at이 RECONCILIATION_TIMEOUT_SECONDS보다 오래됐으면 — 재시작·연결
        끊김·취소 응답 유실 뒤 아무도 해소하지 않은 채 방치된 것으로 보고 이 자리에서
        failed(reconciliation_timeout)로 닫고 슬롯을 연다(feedback.md 확정 정책 —
        별도 background sweep 없음, "같은 사용자의 다음 요청이 잠금 안에서 들어왔을
        때만" 해소). cancel_requested도 같은 취급인 이유: 취소를 요청받은 generation이
        업스트림 취소 확인을 영영 못 받으면(예: 어댑터가 죽거나 응답을 잃어버리면)
        reconciling과 마찬가지로 "종료를 확정할 수 없는 채로 슬롯만 쥐고 있는" 상태라
        같은 구조가 필요하다 — 새 실패 코드를 따로 만들지 않고 기존 지연 해소 경로를
        그대로 재사용한다.
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
        if row["status"] in ("reconciling", "cancel_requested"):
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
        """queued → running 전이. status='queued' 가드가 핵심이다 — 검색(네트워크
        호출) 중에 별도 요청(POST .../cancel)이 먼저 cancel_requested로 바꿔 놨으면
        이 UPDATE는 아무 일도 하지 않는다(0행). 가드 없이 무조건 썼다면 cancel_requested를
        running으로 되돌려 취소 의도를 DB에서 잃어버렸을 것이다 — 그 값을 이 함수의
        반환값으로 알릴 필요는 없다: 이후 어떤 순서로 진행되든 finish_generation이
        같은 행의 "현재" status를 다시 SQL CASE로 직접 보고 최종 상태를 결정하므로,
        여기서 실패해도 최종 결과는 항상 옳다(단일 진실 소스를 finish_generation
        하나로 좁힌다)."""
        with self.pool.connection() as connection, connection.transaction():
            cur = connection.execute(
                """
                UPDATE persona_minimal.generations
                SET status = 'running', citations = %s, input_snapshot = %s, heartbeat_at = now()
                WHERE id = %s AND status = 'queued'
                """,
                (
                    json.dumps([c.to_json() for c in snapshot.citations]),
                    json.dumps(snapshot.to_json()),
                    generation_id,
                ),
            )
            # 실행 상태로 옮긴 같은 트랜잭션에서 소유자와 lease를 다시 기록한다(원자적). 전이가
            # 일어나지 않았으면(취소가 먼저 옴) 소유자 기록도 하지 않는다.
            if cur.rowcount == 1:
                self._stamp_owner(connection, generation_id)

    def finish_generation(
        self, generation_id: UUID, *, status: str, content: str, failure_code: str | None
    ) -> Generation:
        """계약대로 done/error SSE를 보내기 **전에** 호출해 DB에 먼저 반영한다.

        호출자가 계산한 status/failure_code는 "이 자리까지 정상적으로 온 경우"의
        기본값일 뿐이다 — 그 사이 별도 요청(POST .../cancel)이 cancel_requested를
        남겼을 수 있어, 먼저 확정된 취소 의도를 존중해 실제로는 cancelled로 써야
        한다. 이 판단을 파이썬에서 먼저 SELECT로 읽고 나중에 UPDATE하면 그 사이에
        또 경합이 생긴다(TOCTOU) — 하나의 UPDATE 문 안에서 저장돼 있는 현재
        status를 SQL CASE로 직접 보고 결정해 "확인"과 "반영" 사이의 틈을 없앤다.
        WHERE 가드(queued/running/cancel_requested)는 이미 terminal이거나
        reconciling으로 넘어간 행을 또 덮어쓰지 않게 막는다 — 그런 경우 RETURNING이
        빈 결과를 주므로 저장된 실제 값을 다시 읽어 돌려준다."""
        with self.pool.connection() as connection, connection.transaction():
            with connection.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    UPDATE persona_minimal.generations
                    SET status = CASE WHEN status = 'cancel_requested' THEN 'cancelled' ELSE %(status)s END,
                        content = %(content)s,
                        failure_code = CASE WHEN status = 'cancel_requested' THEN NULL
                                            ELSE %(failure_code)s END,
                        finished_at = now()
                    WHERE id = %(id)s AND status = ANY(%(finishable)s)
                    RETURNING id, conversation_id, user_message_id, version_id, mode, status,
                              content, citations, failure_code, retry_of_generation_id,
                              input_snapshot, created_at, finished_at
                    """,
                    {
                        "status": status,
                        "content": content,
                        "failure_code": failure_code,
                        "id": generation_id,
                        "finishable": list(FINISHABLE_STATUSES),
                    },
                )
                row = cur.fetchone()
                if row is None:
                    return self._generation_by_id(cur, generation_id)
                return _generation(row)

    def mark_generation_reconciling(self, generation_id: UUID) -> bool:
        """연결이 끊기거나 Gateway가 재시작될 때 호출한다 — 성공도 실패도 아니라고
        정직하게 표시한다. "프로세스가 죽었으니 failed로 슬롯 해제"는 하지 않는다.

        반환값은 **이 호출이 실제로 상태를 reconciling으로 바꿨는지**다. 이미 terminal
        (completed/failed/cancelled)이면 WHERE 조건에 걸리지 않아 False다. 판단을 선행
        SELECT가 아니라 같은 UPDATE의 rowcount로 하는 이유: 조회와 갱신 사이에
        finish_generation이 끼어들면 "조회 땐 running, 실제로는 이미 completed"가 되어
        바뀌지 않은 상태를 바뀐 것으로 셀 수 있다. 한 문장의 결과는 그 경쟁이 없다.
        """
        with self.pool.connection() as connection, connection.transaction():
            cur = connection.execute(
                """
                UPDATE persona_minimal.generations
                SET status = 'reconciling', heartbeat_at = now()
                WHERE id = %s AND status IN ('queued', 'running', 'cancel_requested')
                """,
                (generation_id,),
            )
            return cur.rowcount == 1

    def reclaim_expired_generations(self) -> int:
        """lease가 만료된(죽은) 소유자의 활성 generation을 reconciling으로 회수한다.

        이전에는 기동 시 소유자를 가리지 않고 활성 행을 전부 바꿨다 — 롤링 중 새 Pod가 살아
        있는 이전 Pod의 스트림을 끊는 원인이었다. 이제 lease가 유효한 행은 건드리지 않고,
        소유자 없는 구버전 행은 LEGACY_OWNERLESS_GRACE_SECONDS가 지난 뒤에만 회수한다.
        회수는 슬롯 해제가 아니다 — reconciling은 기존 300초 규칙을 그대로 따른다.

        반환값은 이 호출이 실제로 바꾼 행 수다. 동시에 여러 인스턴스가 불러도 같은 행은 한
        번만 세어진다(판정과 전환이 한 UPDATE 문장이라서).
        """
        with self.pool.connection() as connection, connection.transaction():
            set_clause, condition = _reclaim_sql(self._lease_schema_available(connection))
            cur = connection.execute(
                f"""
                UPDATE persona_minimal.generations
                SET {set_clause}
                WHERE {condition}
                """,
                {"legacy_grace": LEGACY_OWNERLESS_GRACE_SECONDS},
            )
            return cur.rowcount

    def _replay_before_reclaim(
        self,
        owner_subject: str,
        operation: str,
        scope: str,
        idempotency_key: UUID,
        fingerprint: bytes,
    ) -> Generation | None:
        """회수 전에 idempotency record만 먼저 본다. replay면 기존 generation, 충돌이면
        IdempotencyConflict, 새 요청(또는 사용자·스키마 부재)이면 None.

        사용자 행을 잠근 짧은 트랜잭션이다 — 본 트랜잭션과 같은 잠금 순서라 같은 사용자의 다른
        요청과 직렬화된다. 부재·스키마 오류는 여기서 판정하지 않고 본 트랜잭션에 맡긴다(기존
        오류 응답을 그대로 유지하려는 것이다).
        """
        with (
            self.pool.connection() as connection,
            connection.transaction(),
            connection.cursor(row_factory=dict_row) as cur,
        ):
            if not chat_schema_ready(cur):
                return None
            cur.execute(
                "SELECT subject FROM persona_minimal.users WHERE subject = %s FOR UPDATE",
                (owner_subject,),
            )
            if cur.fetchone() is None:
                return None
            cur.execute(
                """
                SELECT request_fingerprint, result_id
                FROM persona_minimal.chat_idempotency_records
                WHERE owner_subject = %s AND operation = %s AND target_scope = %s
                  AND idempotency_key = %s
                """,
                (owner_subject, operation, scope, idempotency_key),
            )
            record = cur.fetchone()
            if record is None:
                return None
            if bytes(record["request_fingerprint"]) != fingerprint:
                raise IdempotencyConflict
            return self._generation_by_id(cur, record["result_id"])

    def reclaim_user_expired_generations(self, owner_subject: str) -> int:
        """그 사용자의 활성 행 중 lease가 만료된 것을 reconciling으로 회수한다(요청 시 지연 해소).

        기동 말고는 죽은 소유자의 queued/running 행을 풀 경로가 없어, 모든 Pod가 살아 있는
        동안 그 사용자가 영영 막히는 것을 막는다 — background sweep 없이 "그 사용자의 다음
        요청"에서만 해소하는 기존 원칙과 같다. 회수 자체는 슬롯을 열지 않는다: 바뀐 행은
        reconciling이 되고, 이어지는 슬롯 검사가 300초 규칙(마지막 생존 확인 시각 기준,
        _RECLAIM_SET 참고)으로 409 또는 failed(reconciliation_timeout)를 정한다.

        **별도 트랜잭션으로 먼저 커밋한다.** 슬롯 검사는 활성 행이 있으면 GenerationInProgress를
        던지고, 그 예외가 접수 트랜잭션 전체를 ROLLBACK한다 — 같은 트랜잭션에서 회수하면
        회수도 함께 사라진다. 회수 UPDATE는 조건부·멱등이라 따로 커밋해도 안전하다.
        """
        with self.pool.connection() as connection, connection.transaction():
            set_clause, condition = _reclaim_sql(self._lease_schema_available(connection))
            cur = connection.execute(
                f"""
                UPDATE persona_minimal.generations
                SET {set_clause}
                WHERE conversation_id IN (
                    SELECT id FROM persona_minimal.conversations WHERE owner_subject = %(owner)s
                )
                  AND {condition}
                """,
                {"owner": owner_subject, "legacy_grace": LEGACY_OWNERLESS_GRACE_SECONDS},
            )
            reclaimed = cur.rowcount
        if reclaimed:
            metrics.GENERATIONS_RECLAIMED.labels(reason="request_lease_expired").inc(reclaimed)
        return reclaimed

    def extend_own_leases(self) -> list[tuple[UUID, str]]:
        """이 인스턴스가 소유한 활성 행의 lease를 늘리고 (id, status)를 돌려준다.

        인스턴스당 주기마다 한 문장이다 — 대상 행 수는 이 인스턴스가 스트리밍 중인 generation
        수(사용자당 최대 1개)라 작다. 돌려준 status가 cancel_requested면 호출자(lease keeper)가
        로컬 업스트림 취소를 부른다 — 다른 인스턴스로 들어온 cancel 요청을 DB로 전달받는 경로다.
        """
        with self.pool.connection() as connection, connection.transaction():
            if not self._lease_schema_available(connection):
                # 0004(bridge 기간): 연장할 lease가 없다. 실패가 아니므로 메트릭도 올리지 않는다.
                return []
            cur = connection.execute(
                """
                UPDATE persona_minimal.generations
                SET lease_expires_at = now() + make_interval(secs => %s)
                WHERE owner_instance_id = %s
                  AND status IN ('queued', 'running', 'cancel_requested')
                RETURNING id, status
                """,
                (self.lease_seconds, self.instance_id),
            )
            return [(row[0], row[1]) for row in cur.fetchall()]

    def release_own_generations(self) -> int:
        """정상 종료 시 이 인스턴스가 아직 소유한 활성 행을 reconciling으로 넘긴다.

        서버가 연결을 모두 닫은 뒤(lifespan 종료) 부른다. 그 시점에 남은 행은 이 프로세스가 더
        처리하지 않으므로, lease 만료(최대 lease 길이)를 기다리지 않고 바로 "결과를 모름"으로
        남긴다. 다른 인스턴스의 행은 건드리지 않는다. 반환값은 바꾼 행 수다.
        """
        with self.pool.connection() as connection, connection.transaction():
            if not self._lease_schema_available(connection):
                # 0004(bridge 기간): 소유자를 기록하지 않았으므로 "내 행"을 가릴 수 없다. 남은
                # 행은 소유자 없는 행 규칙(240초 유예)으로 다른 인스턴스가 회수한다.
                return 0
            cur = connection.execute(
                """
                UPDATE persona_minimal.generations
                SET status = 'reconciling', heartbeat_at = now()
                WHERE owner_instance_id = %s
                  AND status IN ('queued', 'running', 'cancel_requested')
                """,
                (self.instance_id,),
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
                # 오히려 아는 척하는 것이 된다. heartbeat_at을 여기서도 갱신해야
                # cancel_requested로 고착된 행을 _reject_or_resolve_active_generation의
                # 지연 해소가 "언제부터 멈춰 있었는지" 기준으로 판단할 수 있다.
                cur.execute(
                    """
                    UPDATE persona_minimal.generations
                    SET status = 'cancel_requested', heartbeat_at = now()
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
        # create_generation_queued와 같은 순서(replay·충돌 → 새 요청만 회수 → 본 트랜잭션).
        replayed = self._replay_before_reclaim(
            owner_subject, RETRY_GENERATION_OPERATION, scope, idempotency_key, fingerprint
        )
        if replayed is not None:
            return replayed, True
        self.reclaim_user_expired_generations(owner_subject)
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
                    ) VALUES (%s, %s, %s, %s, %s, 'running', %s, %s, %s)
                    """,
                    (
                        new_generation_id,
                        original["conversation_id"],
                        original["user_message_id"],
                        original["version_id"],
                        # 재시도는 원본의 mode가 아니라 지금 프로세스의 mode로 답한다 —
                        # 그 사이 배포로 업스트림이 바뀌었을 수 있고, 기록은 실제로
                        # 답한 쪽을 가리켜야 한다.
                        self.mode,
                        original["id"],
                        json.dumps(original["input_snapshot"]),
                        json.dumps(original["input_snapshot"]["citations"]),
                    ),
                )
                # retry를 받은 인스턴스가 새 스트림을 소유한다(같은 트랜잭션).
                self._stamp_owner(connection, new_generation_id)
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
