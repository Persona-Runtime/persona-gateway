"""Create the Python-owned minimal API schema.

Revision ID: 0001_persona_minimal
Revises:
Create Date: 2026-09-10
"""

from alembic import op

revision = "0001_persona_minimal"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS persona_minimal")
    op.execute(
        """
        CREATE TABLE persona_minimal.users (
            subject text PRIMARY KEY,
            display_name text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE persona_minimal.personas (
            id uuid PRIMARY KEY,
            owner_subject text NOT NULL
                REFERENCES persona_minimal.users(subject),
            name text NOT NULL,
            deletion_id uuid,
            deleted_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT personas_completed_deletion_has_id
                CHECK (deleted_at IS NULL OR deletion_id IS NOT NULL)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX personas_owner_live_name_key
            ON persona_minimal.personas (owner_subject, name)
            WHERE deleted_at IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX personas_owner_created_idx
            ON persona_minimal.personas (owner_subject, created_at DESC, id DESC)
            WHERE deleted_at IS NULL
        """
    )
    op.execute(
        """
        CREATE TABLE persona_minimal.idempotency_records (
            owner_subject text NOT NULL
                REFERENCES persona_minimal.users(subject),
            operation text NOT NULL,
            target_scope text NOT NULL,
            idempotency_key uuid NOT NULL,
            request_fingerprint bytea NOT NULL,
            persona_id uuid NOT NULL REFERENCES persona_minimal.personas(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (owner_subject, operation, target_scope, idempotency_key)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA persona_minimal CASCADE")
