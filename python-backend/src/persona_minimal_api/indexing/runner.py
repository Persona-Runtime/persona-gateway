"""`start_indexing`이 넘긴 handle로 실제 색인(청킹→임베딩→저장)을 돈다.

`store.start_indexing`이 이미 `editing/ready/failed → processing` 전이와 advisory
lock을 끝낸 뒤 이 모듈을 부른다. 여기서는 그 결과(성공/실패)를 status·error_code·
indexed_revision·indexed_at에 반영하고, 무슨 일이 있어도 advisory lock을 풀고
연결을 pool에 반납한다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from ..repository import IndexingHandle
from .chunker import CHUNKABLE_KINDS, Chunk, chunk_material
from .embedding_client import EmbeddingError, embed
from .store import ChunkRecord, replace_chunks

EMBEDDING_INPUT_TYPE = "passage"


@dataclass(frozen=True)
class _PendingChunk:
    """임베딩을 아직 못 받은 조각. `chunk`에 `source_id`만 더한 것."""

    source_id: UUID
    chunk: Chunk


def run_indexing(handle: IndexingHandle, embedding_base_url: str) -> None:
    """색인을 실행하고 결과를 material_versions에 반영한다. 예외를 밖으로 던지지 않는다.

    실패해도 `material_chunks`와 `indexed_revision`/`indexed_at`은 건드리지 않는다 —
    직전에 성공한 색인이 있었다면 그게 계속 검색에 쓰일 수 있어야 한다(계약의
    핵심 요구사항). 성공했을 때만 그 값들을 이번 시도 결과로 갱신한다.
    """
    try:
        records = _build_records(handle, embedding_base_url)
        replace_chunks(handle.pool, handle.version_id, records)
    except Exception as error:  # noqa: BLE001 - 원인을 구분해 error_code로만 남긴다
        _mark_failed(handle, _classify(error))
    else:
        _mark_ready(handle)
    finally:
        with handle.connection.transaction():
            with handle.connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (str(handle.persona_id),))
        handle.pool.putconn(handle.connection)


def _build_records(handle: IndexingHandle, embedding_base_url: str) -> list[ChunkRecord]:
    pending: list[_PendingChunk] = []
    for source in _load_sources(handle):
        chunks = chunk_material(source["content"], source["kind"])
        if not chunks:
            # 소스가 있는데 조각이 0개면(예: 공백만 있는 본문) 이후 검색에 그 kind가
            # 통째로 빠진다 — 침묵하지 않고 바로 실패시킨다.
            raise _NoChunksProduced(source["kind"])
        pending.extend(_PendingChunk(source_id=source["id"], chunk=chunk) for chunk in chunks)
    if not pending:
        # 소스가 하나도 없으면(전부 선택 항목이라 있을 수 있다) 색인할 것이 없다.
        raise _NoChunksProduced(None)

    result = embed(
        embedding_base_url, [item.chunk.content for item in pending], EMBEDDING_INPUT_TYPE
    )
    if len(result.vectors) != len(pending):
        raise EmbeddingError("임베딩 개수가 조각 수와 다르다")
    model = result.model or ""
    return [
        ChunkRecord(
            id=uuid4(),
            persona_id=handle.persona_id,
            version_id=handle.version_id,
            source_id=item.source_id,
            kind=item.chunk.kind,
            ordinal=item.chunk.ordinal,
            heading_path=item.chunk.heading_path,
            content=item.chunk.content,
            char_count=item.chunk.char_count,
            sha256=hashlib.sha256(item.chunk.content.encode("utf-8")).hexdigest(),
            embedding=vector,
            embedding_model=model,
        )
        for item, vector in zip(pending, result.vectors, strict=True)
    ]


def _load_sources(handle: IndexingHandle) -> list[dict[str, object]]:
    # 여기서 트랜잭션을 명시적으로 열고 닫지 않으면 커밋 안 된 트랜잭션이 암묵적으로
    # 열린 채 남는다 — 그러면 뒤의 `_mark_failed`/`_mark_ready`가 여는
    # `transaction()`이 새 최상위 트랜잭션이 아니라 SAVEPOINT로 중첩되어, 그 UPDATE가
    # 커밋되지 않고 `putconn` 시 psycopg_pool의 리셋에 의해 통째로 롤백된다.
    with handle.connection.transaction():
        with handle.connection.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, kind, content FROM persona_minimal.material_sources "
                "WHERE persona_id = %s AND kind = ANY(%s) ORDER BY kind, created_at, id",
                (handle.persona_id, list(CHUNKABLE_KINDS)),
            )
            return cur.fetchall()


class _NoChunksProduced(Exception):
    def __init__(self, kind: str | None) -> None:
        self.kind = kind
        super().__init__(kind)


def _classify(error: Exception) -> str:
    if isinstance(error, _NoChunksProduced):
        return "no_content" if error.kind is None else "chunking_produced_nothing"
    if isinstance(error, EmbeddingError):
        return "embedding_unavailable"
    return "indexing_failed"


def _mark_ready(handle: IndexingHandle) -> None:
    with handle.connection.transaction():
        with handle.connection.cursor() as cur:
            cur.execute(
                """
                UPDATE persona_minimal.material_versions
                SET status = 'ready', indexed_revision = %s, indexed_at = now(), error_code = NULL
                WHERE persona_id = %s
                """,
                (handle.revision, handle.persona_id),
            )


def _mark_failed(handle: IndexingHandle, error_code: str) -> None:
    with handle.connection.transaction():
        with handle.connection.cursor() as cur:
            cur.execute(
                """
                UPDATE persona_minimal.material_versions
                SET status = 'failed', error_code = %s
                WHERE persona_id = %s
                """,
                (error_code, handle.persona_id),
            )
