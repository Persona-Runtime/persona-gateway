"""Record which Gateway instance owns an active generation and until when.

Revision ID: 0005_generation_lease
Revises: 0004_chat
Create Date: 2026-09-25
"""

from alembic import op

revision = "0005_generation_lease"
down_revision = "0004_chat"
branch_labels = None
depends_on = None

# 활성(아직 끝낼 수 있는) 상태. chat/repository.py의 FINISHABLE_STATUSES와 같아야 한다 —
# 아래 부분 인덱스가 heartbeat 연장과 만료 회수 쿼리의 WHERE와 정확히 맞아야 쓰인다.
_FINISHABLE = "('queued', 'running', 'cancel_requested')"


def upgrade() -> None:
    """generations에 소유자·lease 컬럼을 더한다(api/generation-ownership-lease-design.md).

    새 테이블이 아니라 컬럼 추가다 — 기존 테이블 권한을 그대로 상속하므로 persona-platform
    grants 변경 없이 적용된다. 두 컬럼 모두 NULL 허용이다: 이 revision 이전(또는 호환 릴리스
    구버전 Gateway)이 만든 행은 소유자를 모르며, 그런 행의 회수 규칙은 repository의
    LEGACY_OWNERLESS_GRACE_SECONDS가 따로 정한다(기동 직후 바로 회수하지 않는다).
    """
    op.execute(
        """
        ALTER TABLE persona_minimal.generations
            ADD COLUMN owner_instance_id uuid,
            ADD COLUMN lease_expires_at timestamptz
        """
    )
    # heartbeat: "내 인스턴스의 활성 행 lease 연장" — 인스턴스당 주기마다 한 번 실행된다.
    op.execute(
        "CREATE INDEX generations_active_owner_idx "
        "ON persona_minimal.generations (owner_instance_id) "
        f"WHERE status IN {_FINISHABLE}"
    )
    # 회수: "lease가 만료된 활성 행" — 기동 시·사용자 요청 시 실행된다.
    op.execute(
        "CREATE INDEX generations_active_lease_idx "
        "ON persona_minimal.generations (lease_expires_at) "
        f"WHERE status IN {_FINISHABLE}"
    )


def downgrade() -> None:
    op.execute("DROP INDEX persona_minimal.generations_active_lease_idx")
    op.execute("DROP INDEX persona_minimal.generations_active_owner_idx")
    op.execute(
        """
        ALTER TABLE persona_minimal.generations
            DROP COLUMN lease_expires_at,
            DROP COLUMN owner_instance_id
        """
    )
