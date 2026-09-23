"""Add conversations, user_messages, generations for the Chat API.

Revision ID: 0004_chat
Revises: 0003_material_chunks
Create Date: 2026-09-22
"""

from alembic import op

revision = "0004_chat"
down_revision = "0003_material_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # personas.active_version_id를 여기서 함께 추가한다(2026-09-23).
    #
    # 원래 이 컬럼은 계약(service-api-v1.md §2·§6, openapi의 Persona.active_version_id)에만
    # 있고 어느 migration에도 없었다 — API 응답은 상수 null을 내보내고 있었다. 활성화
    # (draft/activate)를 구현하려면 실제 컬럼이 필요하다.
    #
    # 왜 새 revision(0005)이 아니라 이미 머지된 0004를 고치는가: 0004는 아직 어떤
    # 영속 DB에도 적용되지 않았다(운영 DB는 0003, 테스트는 매번 새 컨테이너). 별도
    # revision으로 나누면 호환 창과 migration Job이 한 번씩 더 필요한데, 얻는 것이
    # 없다. 이 판단은 "아직 적용 전"이 전제이므로, 0004가 한 번이라도 적용된 뒤에는
    # 같은 방식을 쓰지 않는다.
    #
    # NULL 허용이다 — 적용본이 없는 캐릭터가 정상 상태다(자료 입력 전·색인 전).
    # FK는 걸지 않는다: material_versions는 persona당 한 행이라 계속 갱신되므로
    # 참조로 묶으면 "그 캐릭터의 현재 버전"이 되어 적용 시점의 의미를 잃는다
    # (conversations.initial_version_id와 같은 이유, 아래 주석 참고).
    op.execute("ALTER TABLE persona_minimal.personas ADD COLUMN active_version_id uuid")

    # owner_subject를 personas 조인 없이 직접 들고 있는다 — idempotency_records와 같은
    # 이유(denormalize)이고, "사용자당 활성 generation 1개"를 모든 대화에 걸쳐 확인할 때
    # personas를 매번 조인하지 않아도 된다.
    #
    # initial_version_id는 material_versions.version_id를 참조가 아니라 스냅샷으로
    # 담는다 — material_versions는 persona당 한 행이라 계속 갱신되므로, FK로 묶으면
    # "그 캐릭터의 현재 버전"이 되어 대화 생성 시점의 의미를 잃는다.
    op.execute(
        """
        CREATE TABLE persona_minimal.conversations (
            id uuid PRIMARY KEY,
            persona_id uuid NOT NULL
                REFERENCES persona_minimal.personas(id),
            owner_subject text NOT NULL
                REFERENCES persona_minimal.users(subject),
            initial_version_id uuid NOT NULL,
            title text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX conversations_persona_owner_created_idx "
        "ON persona_minimal.conversations (persona_id, owner_subject, created_at DESC, id DESC)"
    )

    op.execute(
        """
        CREATE TABLE persona_minimal.user_messages (
            id uuid PRIMARY KEY,
            conversation_id uuid NOT NULL
                REFERENCES persona_minimal.conversations(id),
            content text NOT NULL
                CHECK (length(content) > 0),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # 메시지는 오래된순으로 읽는다(계약 §2) — 최신순인 conversations와 다르다.
    op.execute(
        "CREATE INDEX user_messages_conversation_created_idx "
        "ON persona_minimal.user_messages (conversation_id, created_at, id)"
    )

    # assistant_message_id는 별도 컬럼이 없다 — API 응답은 이 행의 id를 그대로
    # assistant_message_id로 내려준다. 공개 스키마에 없는 내부 테이블에 같은 값을
    # 중복 저장할 이유가 없다.
    #
    # heartbeat_at: reconciling→failed 지연 해소(마지막 heartbeat/시작 시각 기준
    # 300초)의 기준 시각. 시작 시각으로 초기화하고, reconciling으로 전환할 때 갱신한다.
    # 별도 background sweep은 없다 — 같은 사용자의 다음 요청이 잠금 안에서 이 값을
    # 보고 그 자리에서 판단한다.
    #
    # input_snapshot은 accept 트랜잭션 시점엔 비어 있다(NULL) — 검색은 네트워크 호출이라
    # 트랜잭션 밖(commit 후)에서 돈다. 검색이 끝나면 {question, citations, version_id}로
    # 채우고 status를 running으로 올린다. retry는 이 스냅샷을 그대로 복사해 새
    # generation을 만들고 검색을 다시 돌리지 않는다 — retry 계약이 요구하는 "원래
    # immutable 입력과 버전"을 지키려면, 그 사이 자료가 재색인돼도 다른 청크가 나오면
    # 안 되기 때문이다.
    op.execute(
        """
        CREATE TABLE persona_minimal.generations (
            id uuid PRIMARY KEY,
            conversation_id uuid NOT NULL
                REFERENCES persona_minimal.conversations(id),
            user_message_id uuid NOT NULL
                REFERENCES persona_minimal.user_messages(id),
            version_id uuid NOT NULL,
            mode text NOT NULL
                CHECK (mode IN ('mock', 'llm')),
            status text NOT NULL
                CHECK (status IN ('queued', 'running', 'cancel_requested', 'reconciling',
                                   'completed', 'cancelled', 'failed')),
            content text NOT NULL DEFAULT '',
            citations jsonb NOT NULL DEFAULT '[]'::jsonb,
            failure_code text,
            retry_of_generation_id uuid
                REFERENCES persona_minimal.generations(id),
            input_snapshot jsonb,
            heartbeat_at timestamptz NOT NULL DEFAULT now(),
            created_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX generations_conversation_created_idx "
        "ON persona_minimal.generations (conversation_id, created_at, id)"
    )
    # retry가 "그 user_message의 최신 시도"를 찾을 때, cancel/reconciling 판정이
    # "그 사용자의 활성 generation"을 찾을 때 쓴다.
    op.execute(
        "CREATE INDEX generations_user_message_created_idx "
        "ON persona_minimal.generations (user_message_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX generations_owner_active_idx "
        "ON persona_minimal.generations (conversation_id) "
        "WHERE status IN ('queued', 'running', 'cancel_requested', 'reconciling')"
    )

    # 채팅 3개 작업(대화 생성·chat/completions·retry) 전용 멱등 기록. idempotency_records
    # 테이블을 재사용하지 않는다 — 그 테이블의 결과 컬럼이 persona_id로 고정돼 있어서
    # (그 테이블은 "이 키로 이 캐릭터가 만들어졌다"만 표현한다) 대화/generation을
    # 가리키려면 이름이 맞지 않는다. result_id 하나로 일반화한 병렬 테이블을 둔다.
    op.execute(
        """
        CREATE TABLE persona_minimal.chat_idempotency_records (
            owner_subject text NOT NULL
                REFERENCES persona_minimal.users(subject),
            operation text NOT NULL,
            target_scope text NOT NULL,
            idempotency_key uuid NOT NULL,
            request_fingerprint bytea NOT NULL,
            result_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (owner_subject, operation, target_scope, idempotency_key)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE persona_minimal.chat_idempotency_records")
    op.execute("DROP INDEX persona_minimal.generations_owner_active_idx")
    op.execute("DROP INDEX persona_minimal.generations_user_message_created_idx")
    op.execute("DROP INDEX persona_minimal.generations_conversation_created_idx")
    op.execute("DROP TABLE persona_minimal.generations")
    op.execute("DROP INDEX persona_minimal.user_messages_conversation_created_idx")
    op.execute("DROP TABLE persona_minimal.user_messages")
    op.execute("DROP INDEX persona_minimal.conversations_persona_owner_created_idx")
    op.execute("DROP TABLE persona_minimal.conversations")
    op.execute("ALTER TABLE persona_minimal.personas DROP COLUMN active_version_id")
