"""Add material_chunks for indexed search and material_versions.error_code.

Revision ID: 0003_material_chunks
Revises: 0002_persona_draft
Create Date: 2026-09-17
"""

from alembic import op

revision = "0003_material_chunks"
down_revision = "0002_persona_draft"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pgvector 0.5+는 trusted extension이라 superuser 없이 migrator 계정으로 설치된다.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 인수인계 §4-3 DDL 그대로. profile은 색인하지 않으므로 kind에 없다(항상 프롬프트에
    # 통째로 들어간다). HNSW 인덱스는 일부러 넣지 않는다 — 버전당 수백 행이라 순차 스캔이
    # 충분하고, 실측 p95가 기준을 넘을 때만 추가한다.
    op.execute(
        """
        CREATE TABLE persona_minimal.material_chunks (
            id uuid PRIMARY KEY,
            persona_id uuid NOT NULL
                REFERENCES persona_minimal.personas(id),
            -- material_versions.version_id. 재색인은 이 값으로 이전 조각을 지운다.
            version_id uuid NOT NULL,
            source_id uuid NOT NULL
                REFERENCES persona_minimal.material_sources(id),
            kind text NOT NULL
                CHECK (kind IN ('events', 'relationships', 'abilities', 'speech_examples')),
            ordinal integer NOT NULL
                CHECK (ordinal >= 0),
            heading_path text NOT NULL DEFAULT '',
            content text NOT NULL
                CHECK (length(content) > 0),
            char_count integer NOT NULL,
            sha256 text NOT NULL,
            embedding vector(384) NOT NULL,
            -- 'multilingual-e5-small@<revision>'. 모델이 바뀌면 이 값으로 구분한다.
            embedding_model text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (version_id, kind, source_id, ordinal)
        )
        """
    )
    op.execute(
        "CREATE INDEX material_chunks_lookup "
        "ON persona_minimal.material_chunks (persona_id, version_id, kind)"
    )

    # 색인 실패 사유. status='failed'일 때만 의미가 있고 그 외에는 NULL이다.
    op.execute("ALTER TABLE persona_minimal.material_versions ADD COLUMN error_code text")

    # material_versions는 캐릭터당 한 행이라 status가 "마지막 적용 시도 결과"만 담는다.
    # rev 3 색인 성공 → 편집(rev 4) → rev 4 색인 실패, 이 순서가 되면 material_chunks엔
    # rev 3 조각이 (트랜잭션 롤백 덕에) 그대로 남는데도 status=failed만 보고는 "쓸 수
    # 있는 색인이 있다"를 알 수 없다. indexed_revision·indexed_at은 "실제로 검색에 쓸 수
    # 있는 색인이 어느 revision 것인지"를 status와 분리해서 담는다 — 색인이 성공할 때만
    # runner가 갱신하고, 실패해도 이전 값을 그대로 둔다.
    op.execute("ALTER TABLE persona_minimal.material_versions ADD COLUMN indexed_revision integer")
    op.execute("ALTER TABLE persona_minimal.material_versions ADD COLUMN indexed_at timestamptz")


def downgrade() -> None:
    # 0002와 같은 원칙: 이 revision이 만든 것만 되돌린다. 추가한 역순으로 없앤다.
    op.execute("ALTER TABLE persona_minimal.material_versions DROP COLUMN indexed_at")
    op.execute("ALTER TABLE persona_minimal.material_versions DROP COLUMN indexed_revision")
    op.execute("ALTER TABLE persona_minimal.material_versions DROP COLUMN error_code")
    op.execute("DROP INDEX persona_minimal.material_chunks_lookup")
    op.execute("DROP TABLE persona_minimal.material_chunks")
