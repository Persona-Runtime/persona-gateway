"""Add draft material versions and their sources.

Revision ID: 0002_persona_draft
Revises: 0001_persona_minimal
Create Date: 2026-09-16
"""

from alembic import op

revision = "0002_persona_draft"
down_revision = "0001_persona_minimal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 초안은 캐릭터당 최대 하나다(계약 2절). persona_id를 PK로 두어 DB가 그것을 강제한다.
    # 애플리케이션 검사만으로 두면 동시 요청 둘이 각각 "없음"을 보고 둘 다 만든다.
    #
    # ON DELETE CASCADE를 넣지 않는다. personas는 deleted_at 기반 논리 삭제라 캐릭터를
    # 지워도 행이 남고 CASCADE가 발화하지 않는다. 있으면 정리가 되는 것처럼 오해시킨다.
    #
    # CHECK는 앱 검증의 중복이 아니라 이중 방어다. 앱을 우회한 쓰기도 저장되지 않는다.
    op.execute(
        """
        CREATE TABLE persona_minimal.material_versions (
            persona_id uuid PRIMARY KEY
                REFERENCES persona_minimal.personas(id),
            version_id uuid NOT NULL UNIQUE,
            -- 사용자가 내용을 바꿀 때마다 오른다. PATCH의 expected_revision이 이 값과 겨룬다.
            revision integer NOT NULL
                CHECK (revision >= 1),
            status text NOT NULL
                CHECK (status IN ('editing', 'processing', 'ready', 'failed')),
            -- 처리 job. 저장 전용 경로로 만든 초안은 job이 없다.
            job_id uuid,
            -- 적용본에서 파생했으면 그 version. 새로 시작했으면 NULL.
            base_version_id uuid,
            -- 계약의 Settings. profile은 비어 있으면 안 된다(5절).
            settings_name text NOT NULL
                CHECK (length(settings_name) > 0),
            settings_profile text NOT NULL
                CHECK (length(settings_profile) > 0),
            settings_speech_examples text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # 자료 본문. 검색 대상(events/relationships/abilities)과 설정 입력(profile/speech_examples)이
    # 같은 테이블에 있고 kind로 갈린다. 계약 5절이 정한 다섯 종류 그대로다.
    op.execute(
        """
        CREATE TABLE persona_minimal.material_sources (
            id uuid PRIMARY KEY,
            persona_id uuid NOT NULL
                REFERENCES persona_minimal.material_versions(persona_id),
            kind text NOT NULL
                CHECK (kind IN ('profile', 'events', 'relationships',
                                'abilities', 'speech_examples')),
            -- 표시용 basename. 경로나 명령으로 쓰지 않는다(5절).
            filename text,
            content text NOT NULL
                CHECK (length(content) > 0),
            -- byte_size를 실제 길이와 묶는다. 이게 없으면 byte_size는 그냥 선언한 숫자라,
            -- 1이라고 적고 1 MiB를 넣어도 통과한다. 이 제약이 있어야 5절의 상한이
            -- 실제 저장량의 상한이 된다.
            byte_size integer NOT NULL
                CHECK (byte_size = octet_length(content)),
            sha256 text NOT NULL
                CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # 초안 조회가 매번 자료를 kind 순서로 읽는다.
    op.execute(
        "CREATE INDEX material_sources_persona_kind_idx "
        "ON persona_minimal.material_sources (persona_id, kind, created_at)"
    )


def downgrade() -> None:
    # 0001과 달리 스키마를 통째로 지우지 않는다. 이 revision이 만든 것만 되돌린다.
    op.execute("DROP TABLE persona_minimal.material_sources")
    op.execute("DROP TABLE persona_minimal.material_versions")
