"""계정·세션 테이블(0006_auth_sessions) 읽기·쓰기.

이 모듈은 "무엇을 어떤 잠금 안에서 읽고 쓰는가"만 책임진다. 잠금 횟수·만료 같은 판정은
service.py가 한다 — 판정 규칙을 한곳에 두어 메모리 fake로도 같은 규칙을 검사하기 위해서다.

시각은 모두 DB now()를 기준으로 한다. Pod마다 시계가 조금씩 달라도 만료·잠금 판정이 같은
결과를 내게 하려는 것이다(chat/repository.py의 lease 판정과 같은 이유). 그래서 읽기 결과에
DB 시각(`now`)을 함께 돌려준다.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from psycopg import Cursor
from psycopg.errors import UndefinedTable, UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# 0006 migration의 `username text NOT NULL UNIQUE`에 Postgres가 붙이는 기본 제약 이름.
_USERNAME_UNIQUE_CONSTRAINT = "credentials_username_key"


class UsernameTaken(Exception):
    """같은 username(소문자 정규화 후)의 계정이 이미 있다."""


class AuthSchemaNotReady(Exception):
    """DB가 아직 0005라 credentials·sessions 테이블이 없다.

    A-1 bridge 릴리스 동안(SUPPORTED_ALEMBIC_REVISIONS가 0005·0006 둘 다 허용)에만 생긴다.
    auth 경로는 503으로, 세션 인증은 "세션 없음"으로 처리한다 — 나머지 기능은 영향이 없다.
    """


@contextmanager
def _auth_schema_required() -> Iterator[None]:
    # 미리 alembic_version을 조회하지 않고 쿼리를 그대로 시도한 뒤 UndefinedTable만 바꾼다.
    # 매 요청 조회 한 번을 아끼고, 0006 적용 즉시 재시작 없이 auth가 켜진다(lifespan의
    # material_versions 정리와 같은 방식). 권한 부족 같은 다른 DB 오류는 숨기지 않는다.
    try:
        yield
    except UndefinedTable as error:
        raise AuthSchemaNotReady from error


@dataclass(frozen=True)
class Credential:
    subject: str
    display_name: str
    password_hash: str
    failed_attempts: int
    locked_until: datetime | None


@dataclass(frozen=True)
class SessionRecord:
    subject: str
    display_name: str
    expires_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None
    # 이 행을 읽은 DB 시각. 만료 판정 기준이다.
    now: datetime


class LoginAttempt(Protocol):
    """한 번의 로그인 시도. 열려 있는 동안 그 계정의 credentials 행이 잠겨 있다."""

    # 시도 시작 시각(DB now()). 잠금 판정과 새 locked_until 계산의 기준이다.
    now: datetime
    # 없는 username이면 None.
    credential: Credential | None

    def record_failure(self, failed_attempts: int, locked_until: datetime | None) -> None: ...

    def record_success(self, token_hash: bytes, ttl_seconds: int) -> datetime:
        """실패 기록을 지우고 세션을 발급한다. 반환값은 세션 만료 시각."""
        ...


class AuthStore(Protocol):
    def create_account(
        self,
        subject: str,
        username: str,
        password_hash: str,
        token_hash: bytes,
        ttl_seconds: int,
    ) -> datetime:
        """users·credentials·sessions를 한 트랜잭션으로 만든다. 반환값은 세션 만료 시각.

        username이 이미 있으면 UsernameTaken. 이때 아무 행도 남지 않는다.
        """
        ...

    def login_attempt(self, username: str) -> AbstractContextManager[LoginAttempt]: ...

    def find_session(self, token_hash: bytes) -> SessionRecord | None: ...

    def touch_session(self, token_hash: bytes, min_interval_seconds: int) -> None: ...

    def revoke_session(self, token_hash: bytes) -> None: ...


class PostgresAuthStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    def create_account(
        self,
        subject: str,
        username: str,
        password_hash: str,
        token_hash: bytes,
        ttl_seconds: int,
    ) -> datetime:
        # 세 INSERT가 한 트랜잭션이다. credentials에서 username 중복이 나면 users 행도 함께
        # 롤백되어 "계정 없는 사용자"가 남지 않는다.
        with (
            _auth_schema_required(),
            self.pool.connection() as connection,
            connection.transaction(),
        ):
            with connection.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "INSERT INTO persona_minimal.users(subject, display_name) VALUES (%s, %s)",
                    (subject, username),
                )
                try:
                    cur.execute(
                        """
                        INSERT INTO persona_minimal.credentials(subject, username, password_hash)
                        VALUES (%s, %s, %s)
                        """,
                        (subject, username, password_hash),
                    )
                except UniqueViolation as error:
                    # 동시에 같은 username으로 가입해도 UNIQUE 제약이 하나만 통과시킨다.
                    # 다른 제약 위반(예: 예상 못 한 subject 충돌)은 숨기지 않고 그대로 올린다.
                    if error.diag.constraint_name == _USERNAME_UNIQUE_CONSTRAINT:
                        raise UsernameTaken from error
                    raise
                return _insert_session(cur, token_hash, subject, ttl_seconds)

    @contextmanager
    def login_attempt(self, username: str) -> Iterator[LoginAttempt]:
        """credentials 행을 FOR UPDATE로 잠근 채 로그인 한 번을 처리하게 한다.

        잠그는 이유: 같은 계정에 동시에 틀린 비밀번호가 여러 번 오면, 잠금 없이 "읽고 +1 해서
        쓰기"를 하면 증가분이 서로 덮여 임계치에 늦게 닿는다(무차별 대입 제한이 느슨해진다).
        행 잠금으로 같은 계정의 시도를 한 줄로 세운다. 대신 비밀번호 검증(수십 ms)이 잠금 안에서
        일어나 같은 계정의 동시 로그인은 차례로 처리된다 — 다른 계정에는 영향이 없다.

        블록이 예외 없이 끝나야 기록이 커밋된다.
        """
        with (
            _auth_schema_required(),
            self.pool.connection() as connection,
            connection.transaction(),
        ):
            with connection.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT c.subject, u.display_name, c.password_hash,
                           c.failed_attempts, c.locked_until, now() AS now
                    FROM persona_minimal.credentials AS c
                    JOIN persona_minimal.users AS u ON u.subject = c.subject
                    WHERE c.username = %s
                    FOR UPDATE OF c
                    """,
                    (username,),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute("SELECT now() AS now")
                    now = cur.fetchone()["now"]
                    yield _PostgresLoginAttempt(cur, now, None)
                    return
                credential = Credential(
                    subject=row["subject"],
                    display_name=row["display_name"],
                    password_hash=row["password_hash"],
                    failed_attempts=row["failed_attempts"],
                    locked_until=row["locked_until"],
                )
                yield _PostgresLoginAttempt(cur, row["now"], credential)

    def find_session(self, token_hash: bytes) -> SessionRecord | None:
        with _auth_schema_required(), self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT s.subject, u.display_name, s.expires_at, s.last_seen_at,
                           s.revoked_at, now() AS now
                    FROM persona_minimal.sessions AS s
                    JOIN persona_minimal.users AS u ON u.subject = s.subject
                    WHERE s.token_hash = %s
                    """,
                    (token_hash,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return SessionRecord(**row)

    def touch_session(self, token_hash: bytes, min_interval_seconds: int) -> None:
        # WHERE에 간격 조건을 다시 둔다. 여러 요청이 동시에 "오래됐다"고 판단해도 실제로
        # 바뀌는 건 첫 UPDATE뿐이고, 나머지는 행을 건드리지 않는다.
        with self.pool.connection() as connection:
            connection.execute(
                """
                UPDATE persona_minimal.sessions
                SET last_seen_at = now()
                WHERE token_hash = %s
                  AND (last_seen_at IS NULL
                       OR last_seen_at < now() - make_interval(secs => %s))
                """,
                (token_hash, min_interval_seconds),
            )

    def revoke_session(self, token_hash: bytes) -> None:
        # 이미 취소된 행의 revoked_at은 처음 취소 시각으로 남긴다.
        with self.pool.connection() as connection:
            connection.execute(
                """
                UPDATE persona_minimal.sessions
                SET revoked_at = now()
                WHERE token_hash = %s AND revoked_at IS NULL
                """,
                (token_hash,),
            )


class _PostgresLoginAttempt:
    def __init__(self, cur: Cursor, now: datetime, credential: Credential | None) -> None:
        self._cur = cur
        self.now = now
        self.credential = credential

    def record_failure(self, failed_attempts: int, locked_until: datetime | None) -> None:
        if self.credential is None:
            return
        self._cur.execute(
            """
            UPDATE persona_minimal.credentials
            SET failed_attempts = %s, locked_until = %s, updated_at = now()
            WHERE subject = %s
            """,
            (failed_attempts, locked_until, self.credential.subject),
        )

    def record_success(self, token_hash: bytes, ttl_seconds: int) -> datetime:
        if self.credential is None:
            raise RuntimeError("cannot issue a session for an unknown username")
        self._cur.execute(
            """
            UPDATE persona_minimal.credentials
            SET failed_attempts = 0, locked_until = NULL, updated_at = now()
            WHERE subject = %s
            """,
            (self.credential.subject,),
        )
        return _insert_session(self._cur, token_hash, self.credential.subject, ttl_seconds)


def _insert_session(cur: Cursor, token_hash: bytes, subject: str, ttl_seconds: int) -> datetime:
    cur.execute(
        """
        INSERT INTO persona_minimal.sessions(token_hash, subject, expires_at)
        VALUES (%s, %s, now() + make_interval(secs => %s))
        RETURNING expires_at
        """,
        (token_hash, subject, ttl_seconds),
    )
    return cur.fetchone()["expires_at"]
