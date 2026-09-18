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
        # 조각 교체와 상태 갱신을 한 트랜잭션으로 묶는다 — 따로 커밋하면 그 사이에
        # 프로세스가 죽었을 때 "조각은 새 revision인데 indexed_revision은 이전
        # 값"이라는, 계약이 금지하는 상태가 남을 수 있다. 이 블록 안에서 실패하면
        # 트랜잭션이 통째로 롤백돼 이전 조각이 그대로 남고, 아래 except가 status를
        # failed로 정리한다.
        with handle.connection.transaction():
            replace_chunks(handle.connection, handle.version_id, records)
            _mark_ready_in(handle)
    except Exception as error:  # noqa: BLE001 - 원인을 구분해 error_code로만 남긴다
        _mark_failed(handle, _classify(error))
    finally:
        try:
            handle.connection.execute(
                "SELECT pg_advisory_unlock(hashtext(%s))", (str(handle.persona_id),)
            )
        except Exception:
            # unlock이 실패해도 putconn은 반드시 실행한다 — 여기서 그냥 던지면 아래
            # putconn이 건너뛰어지고 연결이 pool 밖으로 새어 max_size(8)가 영구히
            # 줄어든다. 세션이 끊긴 경우면 서버가 이미 잠금을 풀었다.
            pass
        finally:
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


def _mark_ready_in(handle: IndexingHandle) -> None:
    """호출자가 이미 연 트랜잭션 안에서 실행한다(자체 트랜잭션을 열지 않는다) —
    `replace_chunks`와 한 트랜잭션으로 묶여야 "조각과 indexed_revision은 항상 한
    쌍"이라는 계약을 지킬 수 있다.

    apply는 202를 먼저 돌려주고 색인은 뒤에서 돈다 — 그 사이 PATCH로 revision이
    올라갔으면(rev 5·editing) status는 건드리지 않는다. 이 색인은 이미 낡은
    revision을 위한 것이었지만, 그 revision의 조각·표시는 여전히 유효하므로
    indexed_revision·indexed_at·error_code는 그대로 갱신한다.
    """
    with handle.connection.cursor() as cur:
        cur.execute(
            """
            UPDATE persona_minimal.material_versions
            SET status = CASE WHEN revision = %s THEN 'ready' ELSE status END,
                indexed_revision = %s, indexed_at = now(), error_code = NULL
            WHERE persona_id = %s
            """,
            (handle.revision, handle.revision, handle.persona_id),
        )


def _mark_failed(handle: IndexingHandle, error_code: str) -> None:
    # revision이 일치할 때만 갱신한다 — 그 사이 PATCH로 revision이 올라갔으면
    # (rev 5·editing) 이 실패는 이미 낡은 시도에 대한 것이라 editing을 failed로
    # 덮으면 안 된다. WHERE에 revision을 더해 그 경우 조용히 no-op이 되게 한다.
    with handle.connection.transaction():
        with handle.connection.cursor() as cur:
            cur.execute(
                """
                UPDATE persona_minimal.material_versions
                SET status = 'failed', error_code = %s
                WHERE persona_id = %s AND revision = %s
                """,
                (error_code, handle.persona_id, handle.revision),
            )
