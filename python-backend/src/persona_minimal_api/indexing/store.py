"""material_chunks에 색인 조각을 쓴다 — 인수인계 §4-3/§4-2.

이 모듈은 이미 임베딩까지 끝난 레코드만 받는다. `chunker.Chunk`에는 없는
`id`·`persona_id`·`source_id`·`sha256`·`embedding`·`embedding_model`을 채우는 건
(다음다음 라운드의) `runner.py`의 책임이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg_pool import ConnectionPool

# chunker.py의 접두 구분자와 같은 문자열을 쓴다 — DDL의 heading_path는 text라
# tuple을 그대로 저장할 수 없고, [heading > path] 접두와 다른 구분자를 쓰면
# 사람이 두 값을 나란히 봤을 때 같은 경로인지 헷갈린다.
HEADING_PATH_SEPARATOR = " > "


@dataclass(frozen=True)
class ChunkRecord:
    """material_chunks 한 행. 저장 직전 상태 — 검증은 여기서 하지 않는다."""

    id: UUID
    persona_id: UUID
    version_id: UUID
    source_id: UUID
    kind: str
    ordinal: int
    heading_path: tuple[str, ...]
    content: str
    char_count: int
    sha256: str
    embedding: list[float]
    embedding_model: str


def replace_chunks(pool: ConnectionPool, version_id: UUID, records: list[ChunkRecord]) -> None:
    """`version_id`의 기존 조각을 지우고 `records`를 한 트랜잭션에서 새로 넣는다.

    재색인은 DELETE 후 INSERT다 — 행을 골라 고치지 않는다. 트랜잭션 안이므로 중간에
    실패하면 이전 조각이 그대로 남는다(부분적으로 지워진 채 끝나지 않는다).

    UNIQUE (version_id, kind, source_id, ordinal)는 같은 배치 안에서 ordinal이
    겹치는 호출자 실수를 잡는 안전망이다 — DELETE가 먼저 실행되므로 정상 흐름에서는
    기존 행과 충돌할 일이 없고, 그래서 ON CONFLICT 처리를 두지 않는다.
    """
    # pgvector 타입 어댑터는 pool 생성 시(repository.create_pool의 configure=register_vector)
    # 이미 모든 연결에 등록돼 있다 — 여기서 다시 등록하지 않는다.
    with pool.connection() as connection, connection.transaction():
        with connection.cursor() as cur:
            cur.execute(
                "DELETE FROM persona_minimal.material_chunks WHERE version_id = %s",
                (version_id,),
            )
            for record in records:
                cur.execute(
                    """
                    INSERT INTO persona_minimal.material_chunks
                        (id, persona_id, version_id, source_id, kind, ordinal,
                         heading_path, content, char_count, sha256, embedding,
                         embedding_model)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record.id,
                        record.persona_id,
                        record.version_id,
                        record.source_id,
                        record.kind,
                        record.ordinal,
                        HEADING_PATH_SEPARATOR.join(record.heading_path),
                        record.content,
                        record.char_count,
                        record.sha256,
                        record.embedding,
                        record.embedding_model,
                    ),
                )
