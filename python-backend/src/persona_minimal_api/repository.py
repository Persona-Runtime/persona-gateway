from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg import Error as PsycopgError
from psycopg_pool import ConnectionPool
from psycopg_pool import PoolTimeout

from .cursor import PersonaCursor

MAX_PERSONAS_PER_USER = 3
CREATE_PERSONA_OPERATION = "create_persona"
CREATE_PERSONA_SCOPE = "/v1/personas"
REQUIRED_ALEMBIC_REVISION = "0001_persona_minimal"


class SafePoolLogFilter(logging.Filter):
    """psycopg pool의 원문 연결 오류가 운영 로그에 남지 않게 한다."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            record.msg = "database pool connection unavailable"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


_POOL_LOG_FILTER = SafePoolLogFilter()


def configure_pool_logging() -> None:
    logger = logging.getLogger("psycopg.pool")
    if _POOL_LOG_FILTER not in logger.filters:
        logger.addFilter(_POOL_LOG_FILTER)


class DuplicatePersonaName(Exception):
    pass


class PersonaLimitExceeded(Exception):
    pass


class IdempotencyConflict(Exception):
    pass


@dataclass(frozen=True)
class Persona:
    id: UUID
    name: str
    created_at: datetime
    deletion_id: UUID | None
    deleted_at: datetime | None

    @property
    def status(self) -> str:
        return (
            "deleting"
            if self.deletion_id is not None and self.deleted_at is None
            else "needs_material"
        )


class PersonaStore(Protocol):
    def list_personas(
        self, owner_subject: str, limit: int, cursor: PersonaCursor | None
    ) -> list[Persona]: ...

    def create_persona(
        self, owner_subject: str, display_name: str, name: str, idempotency_key: UUID
    ) -> Persona: ...


@runtime_checkable
class ReadinessStore(Protocol):
    def is_ready(self) -> bool: ...


def fingerprint_name(name: str) -> bytes:
    return hashlib.sha256(name.encode("utf-8")).digest()


def _persona(row: dict[str, object]) -> Persona:
    return Persona(
        id=row["id"],  # type: ignore[arg-type]
        name=row["name"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        deletion_id=row["deletion_id"],  # type: ignore[arg-type]
        deleted_at=row["deleted_at"],  # type: ignore[arg-type]
    )


class PostgresPersonaStore:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def list_personas(
        self, owner_subject: str, limit: int, cursor: PersonaCursor | None
    ) -> list[Persona]:
        query = """
            SELECT id, name, created_at, deletion_id, deleted_at
            FROM persona_minimal.personas
            WHERE owner_subject = %s AND deleted_at IS NULL
        """
        values: list[object] = [owner_subject]
        if cursor is not None:
            query += " AND (created_at, id) < (%s, %s)"
            values.extend([cursor.created_at, cursor.persona_id])
        query += " ORDER BY created_at DESC, id DESC LIMIT %s"
        values.append(limit)
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cur:
                cur.execute(query, values)
                return [_persona(row) for row in cur.fetchall()]

    def create_persona(
        self, owner_subject: str, display_name: str, name: str, idempotency_key: UUID
    ) -> Persona:
        fingerprint = fingerprint_name(name)
        with self.pool.connection() as connection, connection.transaction():
            with connection.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    INSERT INTO persona_minimal.users(subject, display_name)
                    VALUES (%s, %s)
                    ON CONFLICT (subject) DO UPDATE
                    SET display_name = EXCLUDED.display_name, updated_at = now()
                    """,
                    (owner_subject, display_name),
                )
                cur.execute(
                    # 사용자별 생성 요청을 직렬화해 count 검사와 등록 사이에 3개 한도가 깨지지 않게 한다.
                    "SELECT subject FROM persona_minimal.users WHERE subject = %s FOR UPDATE",
                    (owner_subject,),
                )
                cur.fetchone()
                cur.execute(
                    """
                    SELECT request_fingerprint, persona_id
                    FROM persona_minimal.idempotency_records
                    WHERE owner_subject = %s AND operation = %s AND target_scope = %s
                      AND idempotency_key = %s
                    """,
                    (
                        owner_subject,
                        CREATE_PERSONA_OPERATION,
                        CREATE_PERSONA_SCOPE,
                        idempotency_key,
                    ),
                )
                record = cur.fetchone()
                # 이미 성공한 같은 키는 한도 도달 뒤에도 같은 결과를 돌려야 하므로 한도 검사보다 먼저 본다.
                if record is not None:
                    if bytes(record["request_fingerprint"]) != fingerprint:
                        raise IdempotencyConflict
                    cur.execute(
                        """
                        SELECT id, name, created_at, deletion_id, deleted_at
                        FROM persona_minimal.personas WHERE id = %s
                        """,
                        (record["persona_id"],),
                    )
                    return _persona(cur.fetchone())

                cur.execute(
                    """
                    SELECT count(*) AS count FROM persona_minimal.personas
                    WHERE owner_subject = %s AND deleted_at IS NULL
                    """,
                    (owner_subject,),
                )
                if cur.fetchone()["count"] >= MAX_PERSONAS_PER_USER:
                    raise PersonaLimitExceeded
                cur.execute(
                    """
                    SELECT 1 FROM persona_minimal.personas
                    WHERE owner_subject = %s AND name = %s AND deleted_at IS NULL
                    """,
                    (owner_subject, name),
                )
                if cur.fetchone() is not None:
                    raise DuplicatePersonaName

                persona_id = uuid4()
                cur.execute(
                    """
                    INSERT INTO persona_minimal.personas(id, owner_subject, name)
                    VALUES (%s, %s, %s)
                    RETURNING id, name, created_at, deletion_id, deleted_at
                    """,
                    (persona_id, owner_subject, name),
                )
                persona = _persona(cur.fetchone())
                cur.execute(
                    """
                    INSERT INTO persona_minimal.idempotency_records(
                        owner_subject, operation, target_scope, idempotency_key,
                        request_fingerprint, persona_id
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        owner_subject,
                        CREATE_PERSONA_OPERATION,
                        CREATE_PERSONA_SCOPE,
                        idempotency_key,
                        fingerprint,
                        persona.id,
                    ),
                )
                return persona

    def is_ready(self) -> bool:
        """필수 테이블과 migration revision을 짧게 확인한다.

        liveness는 DB 장애로 실패하면 안 된다. readiness만 이 검사를 사용하고,
        예상 가능한 연결·권한·스키마 오류는 외부에 세부 정보를 노출하지 않고 false로 바꾼다.
        """
        deadline = time.monotonic() + self.pool.timeout
        try:
            with self.pool.connection(timeout=_remaining_seconds(deadline)) as connection:
                with connection.transaction(), connection.cursor() as cur:
                    # Pool 대기 뒤 남은 시간만 DB statement에 준다. probe timeout은 실행 중인
                    # 동기 SQL을 취소하지 못하므로, DB가 직접 잠금 대기를 끊어야 한다.
                    cur.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (_statement_timeout_value(deadline),),
                    )
                    cur.execute(
                        """
                        SELECT
                            to_regclass('persona_minimal.personas') IS NOT NULL
                            AND to_regclass('persona_minimal.alembic_version') IS NOT NULL
                            AND EXISTS (
                                SELECT 1
                                FROM persona_minimal.alembic_version
                                WHERE version_num = %s
                            )
                        """,
                        (REQUIRED_ALEMBIC_REVISION,),
                    )
                    row = cur.fetchone()
                    return row is not None and bool(row[0])
        except (PoolTimeout, PsycopgError):
            return False


def _remaining_seconds(deadline: float) -> float:
    return max(0.001, deadline - time.monotonic())


def _statement_timeout_value(deadline: float) -> str:
    return f"{max(1, int(_remaining_seconds(deadline) * 1000))}ms"


def create_pool(database_url: str, timeout_seconds: float) -> ConnectionPool:
    configure_pool_logging()
    pool = ConnectionPool(
        conninfo=database_url,
        kwargs={
            "connect_timeout": max(1, round(timeout_seconds)),
            "options": f"-c statement_timeout={max(1, int(timeout_seconds * 1000))}",
        },
        min_size=1,
        max_size=8,
        timeout=timeout_seconds,
        open=False,
    )
    # DB가 꺼져 있어도 healthz를 제공해야 하므로, 기동 중 연결 성공을 기다리지 않는다.
    pool.open(wait=False)
    return pool
