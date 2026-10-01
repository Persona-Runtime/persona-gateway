"""Add local accounts (username + password hash) and opaque session tokens.

Revision ID: 0006_auth_sessions
Revises: 0005_generation_lease
Create Date: 2026-10-01
"""

from alembic import op

revision = "0006_auth_sessions"
down_revision = "0005_generation_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """gateway가 직접 들고 있는 계정·세션 테이블 두 개를 더한다.

    기존 테이블·컬럼은 건드리지 않는다. 다만 **새 테이블**이라 0005와 달리 기존 권한을 상속하지
    않는다 — persona-platform `db/grants/persona_minimal.sql`에 두 테이블의 SELECT·INSERT·UPDATE를
    추가해야 앱 계정이 쓸 수 있다(권한이 없으면 가입·로그인·세션 인증이 500이 된다).

    - credentials: 사용자당 비밀번호 하나. username은 저장 전에 소문자로 정규화되어 UNIQUE가
      곧 대소문자 무시 중복 검사다. password_hash는 argon2id PHC 문자열(원문 저장 금지).
    - sessions: 토큰 원문이 아니라 sha256(token)만 저장한다. DB가 유출돼도 해시로는 Bearer
      헤더를 만들 수 없다. 만료는 발급 시 고정하고 활동으로 늘리지 않는다.
    """
    op.execute(
        """
        CREATE TABLE persona_minimal.credentials (
            subject         text PRIMARY KEY REFERENCES persona_minimal.users(subject),
            username        text NOT NULL UNIQUE,
            password_hash   text NOT NULL,
            failed_attempts integer NOT NULL DEFAULT 0,
            locked_until    timestamptz,
            created_at      timestamptz NOT NULL DEFAULT now(),
            updated_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE persona_minimal.sessions (
            token_hash    bytea PRIMARY KEY,
            subject       text NOT NULL REFERENCES persona_minimal.users(subject),
            created_at    timestamptz NOT NULL DEFAULT now(),
            expires_at    timestamptz NOT NULL,
            last_seen_at  timestamptz,
            revoked_at    timestamptz
        )
        """
    )
    # 사용자별 세션 정리·조회(예: 모든 세션 취소, 만료 세션 청소)용. 인증 경로는 PK로 찾는다.
    op.execute(
        "CREATE INDEX sessions_subject_expires_idx "
        "ON persona_minimal.sessions (subject, expires_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE persona_minimal.sessions")
    op.execute("DROP TABLE persona_minimal.credentials")
