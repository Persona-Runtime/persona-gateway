"""material_chunks에서 pgvector 코사인 유사도로 조각을 찾는다 — 인수인계 §Phase 4.

`indexing/store.py`가 쓰기 쪽 저수준 모듈이듯, 이 모듈은 읽기 쪽 저수준 모듈이다 —
`PersonaStore`(repository.py)를 거치지 않고 `Connection`/`ConnectionPool`을 직접 받는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from pgvector import Vector
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ..indexing.embedding_client import embed
from ..repository import NotIndexed, PersonaNotFound, require_draft_pointer_schema
from .metrics import RETRIEVAL_SECONDS

# §8 Q5 확정 — 골든셋 전 변경 금지.
BODY_KINDS = ("events", "relationships", "abilities")
SPEECH_KINDS = ("speech_examples",)
BODY_K = 5
SPEECH_K = 5


@dataclass(frozen=True)
class RetrievedChunk:
    id: UUID
    kind: str
    source_id: UUID
    ordinal: int
    heading_path: str
    content: str
    char_count: int
    score: float


def search(
    connection: Connection,
    *,
    persona_id: UUID,
    version_id: UUID,
    query_vector: list[float],
    kinds: tuple[str, ...],
    k: int,
) -> list[RetrievedChunk]:
    """persona_id·version_id·kinds는 기본값 없이 항상 명시해야 한다 — 특히
    version_id를 빠뜨리면 낡은 revision이나(재색인 후에도 이전 조각이 그대로
    있는 경우) 다른 캐릭터의 조각이 섞여 나올 수 있다.
    """
    # INSERT와 달리(대상 컬럼 타입이 vector라 암시적 assignment cast가 통한다), <=>
    # 연산자 안의 파라미터는 대상 컬럼 타입 문맥이 없어 그냥 리스트를 넘기면 psycopg가
    # double precision[]로 바인딩한다 — vector <=> double precision[] 연산자가 없어
    # UndefinedFunction이 난다. Vector로 명시적으로 감싸야 vector 타입으로 바인딩된다.
    vector_param = Vector(query_vector)
    with connection.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, kind, source_id, ordinal, heading_path, content, char_count,
                   1 - (embedding <=> %s) AS score
            FROM persona_minimal.material_chunks
            WHERE persona_id = %s AND version_id = %s AND kind = ANY(%s)
            ORDER BY embedding <=> %s
            LIMIT %s
            """,
            (vector_param, persona_id, version_id, list(kinds), vector_param, k),
        )
        return [RetrievedChunk(**row) for row in cur.fetchall()]


def load_indexed_version(
    pool: ConnectionPool, owner_subject: str, persona_id: UUID
) -> tuple[UUID, int]:
    """호출자가 version을 고르지 않았을 때 검색할 version과 그 indexed_revision을 고른다.

    적용본이 있으면 적용본을, 없으면 초안을 고른다 — 캐릭터에 version이 여럿일 수
    있게 되면서 "그 캐릭터의 version"만으로는 대상이 정해지지 않기 때문이다. 적용본을
    먼저 보는 이유는 대화가 쓰는 것이 적용본이라서다. 초안까지 보는 이유는 활성화
    전에도 색인 결과를 확인할 수 있어야 하기 때문이다(/retrieve 디버그 엔드포인트).

    잠그지 않는다(FOR UPDATE 없이) — 검색은 쓰기가 아니다.
    """
    with pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cur:
            require_draft_pointer_schema(cur)
            cur.execute(
                "SELECT mv.version_id, mv.indexed_revision "
                "FROM persona_minimal.personas AS p "
                "LEFT JOIN persona_minimal.material_versions AS mv "
                "  ON mv.version_id = COALESCE(p.active_version_id, p.draft_version_id) "
                "WHERE p.id = %s AND p.owner_subject = %s AND p.deleted_at IS NULL",
                (persona_id, owner_subject),
            )
            row = cur.fetchone()
    if row is None:
        raise PersonaNotFound
    # 캐릭터는 있는데 가리키는 version이 없다(자료를 아직 안 넣었다). LEFT JOIN이라
    # 행은 오고 값만 NULL이다.
    if row["version_id"] is None or row["indexed_revision"] is None:
        raise NotIndexed
    return row["version_id"], row["indexed_revision"]


def load_indexed_revision(
    pool: ConnectionPool, owner_subject: str, persona_id: UUID, version_id: UUID
) -> int:
    """호출자가 이미 고른 version의 indexed_revision을 읽는다.

    version을 다시 고르지 않는다 — 채팅 생성은 접수 시점의 적용본(generations.
    version_id)으로 답해야 하는데, 그 사이 재활성화가 일어나면 캐릭터의 "지금 적용본"은
    다른 version이다. 소유자 조건은 그대로 건다(version_id만으로 남의 자료를 읽을 수
    있으면 안 된다).
    """
    with pool.connection() as connection:
        with connection.cursor(row_factory=dict_row) as cur:
            require_draft_pointer_schema(cur)
            cur.execute(
                "SELECT mv.indexed_revision "
                "FROM persona_minimal.material_versions AS mv "
                "JOIN persona_minimal.personas AS p ON p.id = mv.persona_id "
                "WHERE mv.version_id = %s AND mv.persona_id = %s "
                "  AND p.owner_subject = %s AND p.deleted_at IS NULL",
                (version_id, persona_id, owner_subject),
            )
            row = cur.fetchone()
    if row is None:
        raise PersonaNotFound
    if row["indexed_revision"] is None:
        raise NotIndexed
    return row["indexed_revision"]


@dataclass(frozen=True)
class RetrievedContext:
    indexed_revision: int
    body: list[RetrievedChunk]
    speech: list[RetrievedChunk]


def retrieve_context(
    pool: ConnectionPool,
    embedding_base_url: str,
    *,
    owner_subject: str,
    persona_id: UUID,
    question: str,
    version_id: UUID | None = None,
) -> RetrievedContext:
    """질문 임베딩 1회 + 본문/대사 검색 2회를 묶는다. 질문 원문은 로그·예외
    메시지에 넣지 않는다.

    version_id를 주면 그 버전의 조각만 검색한다 — 채팅 생성은 접수 시점에 고른
    적용본(generations.version_id)을 넘겨, 검색이 그 사이 바뀐 다른 버전을 읽지
    않게 한다(계약 §7 "서버가 현재 적용 version을 선택한다"). 안 주면 색인된
    버전을 직접 찾는다(/retrieve 디버그 엔드포인트가 그 경로를 쓴다).
    """
    if version_id is None:
        version_id, indexed_revision = load_indexed_version(pool, owner_subject, persona_id)
    else:
        # 호출자가 이미 버전을 골랐다 — indexed_revision은 표시용 정보라 함께 읽되,
        # **그 버전의** 값을 읽는다(캐릭터의 지금 적용본이 아니다).
        indexed_revision = load_indexed_revision(pool, owner_subject, persona_id, version_id)
    result = embed(embedding_base_url, [question], "query")
    query_vector = result.vectors[0]
    with pool.connection() as connection:
        with RETRIEVAL_SECONDS.labels(kind_group="body").time():
            body = search(
                connection,
                persona_id=persona_id,
                version_id=version_id,
                query_vector=query_vector,
                kinds=BODY_KINDS,
                k=BODY_K,
            )
        with RETRIEVAL_SECONDS.labels(kind_group="speech").time():
            speech = search(
                connection,
                persona_id=persona_id,
                version_id=version_id,
                query_vector=query_vector,
                kinds=SPEECH_KINDS,
                k=SPEECH_K,
            )
    return RetrievedContext(indexed_revision=indexed_revision, body=body, speech=speech)
