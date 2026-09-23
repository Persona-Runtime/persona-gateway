"""Add the Chat API tables and make material version a first-class key.

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
    _upgrade_version_as_first_class_key()
    _upgrade_chat_tables()


def _upgrade_version_as_first_class_key() -> None:
    """자료 버전을 "캐릭터당 한 행"에서 독립 엔티티로 올린다(2026-09-23).

    왜 필요한가: 계약 §6은 활성화 성공 뒤 초안 슬롯을 비워 다음 수정을 허용하라고
    정하는데, 0002 스키마로는 그럴 수 없다. material_versions의 PK가 persona_id라
    캐릭터당 행이 하나뿐이고, material_sources가 그 persona_id를 참조한다 — 초안과
    적용본이 같은 한 행을 공유하므로 초안을 고치면 적용본이 따라 바뀌고, 초안을
    지우면 적용본이 쓰던 자료와 색인 조각이 함께 사라진다.

    왜 새 revision(0005)이 아니라 이미 머지된 0004를 고치는가: 0004는 아직 어떤
    영속 DB에도 적용되지 않았다(운영 DB는 0003, 테스트는 매번 새 컨테이너). 별도
    revision으로 나누면 호환 창과 migration Job이 한 번씩 더 필요한데, 얻는 것이
    없다. 이 판단은 "아직 적용 전"이 전제이므로, 0004가 한 번이라도 적용된 뒤에는
    같은 방식을 쓰지 않는다.

    제약 이름은 전부 PostgreSQL 기본값이다(0001만 제약에 이름을 직접 붙였다).
    기본 이름에 기대는 DROP이 여기 여럿 있으므로, round-trip 테스트가
    information_schema로 이름의 실재를 단언해 가정이 깨지면 먼저 실패하게 한다.
    """
    # --- 1. material_sources를 version에 매단다 ---------------------------
    # 순서가 중요하다: 이 FK가 material_versions의 PK(persona_id)에 의존하므로,
    # 먼저 끊지 않으면 아래 PK 교체가 "다른 객체가 의존한다"로 거절된다.
    op.execute(
        "ALTER TABLE persona_minimal.material_sources "
        "DROP CONSTRAINT material_sources_persona_id_fkey"
    )
    op.execute("ALTER TABLE persona_minimal.material_sources ADD COLUMN version_id uuid")
    # 기존 행은 캐릭터당 version이 하나뿐이라 대응이 유일하다.
    op.execute(
        """
        UPDATE persona_minimal.material_sources AS s
        SET version_id = v.version_id
        FROM persona_minimal.material_versions AS v
        WHERE v.persona_id = s.persona_id
        """
    )
    op.execute("ALTER TABLE persona_minimal.material_sources ALTER COLUMN version_id SET NOT NULL")
    # 인덱스를 컬럼보다 먼저 지운다. DROP COLUMN이 그 컬럼에 딸린 인덱스를 같이
    # 없애 주지만, 그 자동 동작에 기대면 이 migration을 읽는 사람이 0002의 인덱스가
    # 어디서 사라졌는지 알 수 없다.
    op.execute("DROP INDEX persona_minimal.material_sources_persona_kind_idx")
    # persona_id는 **지운다.** version_id로 완전히 유도되는 값이라 두 컬럼이 어긋날
    # 수 있고(같은 캐릭터의 다른 version에 소스를 잘못 매다는 경로가 생긴다), 무엇보다
    # 이 변경의 목적이 "persona 기준으로 자료를 읽는 코드"를 없애는 것이다. 컬럼이
    # 남아 있으면 그런 질의가 계속 컴파일된다. material_chunks가 persona_id를 그대로
    # 들고 있는 것과 다른 판단인 이유: 그쪽은 검색 질의가 persona_id·version_id를
    # 함께 걸어 "다른 캐릭터 조각이 섞이지 않음"을 DB 수준에서 한 번 더 확인하는
    # 안전망이고, 자료 소유는 version 하나로 충분하다.
    op.execute("ALTER TABLE persona_minimal.material_sources DROP COLUMN persona_id")

    # --- 2. material_versions의 PK를 version_id로 옮긴다 ------------------
    op.execute(
        "ALTER TABLE persona_minimal.material_versions DROP CONSTRAINT material_versions_pkey"
    )
    op.execute(
        "ALTER TABLE persona_minimal.material_versions "
        "DROP CONSTRAINT material_versions_version_id_key"
    )
    op.execute(
        "ALTER TABLE persona_minimal.material_versions "
        "ADD CONSTRAINT material_versions_pkey PRIMARY KEY (version_id)"
    )
    # persona_id는 이제 "이 버전이 어느 캐릭터의 것인가"만 뜻한다. 유일하지 않으므로
    # 조회용 인덱스로 대신한다(캐릭터 삭제·감사 때 그 캐릭터의 version을 훑는다).
    op.execute(
        "CREATE INDEX material_versions_persona_created_idx "
        "ON persona_minimal.material_versions (persona_id, created_at)"
    )

    op.execute(
        "ALTER TABLE persona_minimal.material_sources "
        "ADD CONSTRAINT material_sources_version_id_fkey "
        "FOREIGN KEY (version_id) REFERENCES persona_minimal.material_versions(version_id)"
    )
    # 초안 조회가 자료를 kind 순서로 읽는다(0002의 같은 인덱스를 version 기준으로 옮긴 것).
    op.execute(
        "CREATE INDEX material_sources_version_kind_idx "
        "ON persona_minimal.material_sources (version_id, kind, created_at)"
    )

    # --- 3. personas가 "지금 초안"과 "적용본"을 각각 가리킨다 --------------
    # 초안 슬롯이 컬럼 하나가 되면서 활성화는 행을 옮기는 대신 포인터만 바꾼다:
    # active_version_id := draft_version_id, draft_version_id := NULL. 적용본의 자료와
    # 색인 조각은 그 자리에 그대로 남고, 다음 수정은 새 version 행에서 시작한다.
    #
    # 둘 다 NULL 허용이다 — 자료를 아직 안 넣은 캐릭터(둘 다 NULL), 편집 중(초안만),
    # 활성화 직후(적용본만), 재편집 중(둘 다)이 모두 정상 상태다.
    #
    # active_version_id는 계약(service-api-v1.md §2·§6, openapi의
    # Persona.active_version_id)에만 있고 어느 migration에도 없던 컬럼이다 — API 응답은
    # 상수 null을 내보내고 있었다.
    op.execute("ALTER TABLE persona_minimal.personas ADD COLUMN active_version_id uuid")
    op.execute("ALTER TABLE persona_minimal.personas ADD COLUMN draft_version_id uuid")
    # 지금 있는 material_versions 행은 전부 초안이다(활성화 기능이 없던 시절의 데이터).
    op.execute(
        """
        UPDATE persona_minimal.personas AS p
        SET draft_version_id = v.version_id
        FROM persona_minimal.material_versions AS v
        WHERE v.persona_id = p.id
        """
    )
    # 이제 FK를 걸 수 있다. version 행은 만들어진 뒤 persona_id·version_id가 바뀌지
    # 않고 초안마다 새로 생기므로, 참조가 "그 캐릭터의 현재 버전"으로 미끄러지지
    # 않는다 — conversations.initial_version_id를 스냅샷으로 둔 이유가 여기서는
    # 해소된다. FK가 있으면 없는 version을 가리키는 포인터가 아예 저장되지 않는다.
    op.execute(
        "ALTER TABLE persona_minimal.personas "
        "ADD CONSTRAINT personas_active_version_id_fkey "
        "FOREIGN KEY (active_version_id) REFERENCES persona_minimal.material_versions(version_id)"
    )
    op.execute(
        "ALTER TABLE persona_minimal.personas "
        "ADD CONSTRAINT personas_draft_version_id_fkey "
        "FOREIGN KEY (draft_version_id) REFERENCES persona_minimal.material_versions(version_id)"
    )

    # --- 4. 초안 생성 멱등 기록이 만든 version을 직접 가리킨다 -------------
    # 계약 §4: 같은 Idempotency-Key 재전송은 언제나 같은 결과를 돌려줘야 한다. 초안
    # 생성 replay가 personas.draft_version_id를 다시 읽으면, 그 사이 활성화가 일어나
    # 포인터가 NULL이 된 순간 같은 키가 404로 바뀐다. 그때 만든 version을 기록해 두면
    # 포인터와 무관하게 같은 응답을 재현할 수 있다.
    #
    # FK는 걸지 않는다 — 초안을 폐기하면(discard) 그 version 행은 사라지는데, 멱등
    # 기록은 "이 키는 이미 처리했다"는 사실로서 남아야 하기 때문이다.
    # 기존 행(캐릭터 생성 기록)은 NULL로 남는다.
    op.execute("ALTER TABLE persona_minimal.idempotency_records ADD COLUMN version_id uuid")


def _upgrade_chat_tables() -> None:
    # owner_subject를 personas 조인 없이 직접 들고 있는다 — idempotency_records와 같은
    # 이유(denormalize)이고, "사용자당 활성 generation 1개"를 모든 대화에 걸쳐 확인할 때
    # personas를 매번 조인하지 않아도 된다.
    #
    # initial_version_id는 material_versions.version_id를 참조가 아니라 스냅샷으로
    # 담는다. 대화가 가리키는 것은 언제나 활성화된 version이고 활성화 뒤에는 그 행으로
    # 가는 수정 경로가 없으므로 FK를 걸 수도 있지만, 걸지 않는다 — 오래된 version을
    # 정리하는 후속 과제가 대화 기록 때문에 막히면 안 되고, 대화가 담는 것은 "그때
    # 무엇으로 답했는가"라는 기록이지 지금도 살아 있어야 하는 대상이 아니다.
    # (generations.version_id도 같은 이유로 참조가 아니다.)
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
    _downgrade_chat_tables()
    _downgrade_version_as_first_class_key()


def _downgrade_chat_tables() -> None:
    op.execute("DROP TABLE persona_minimal.chat_idempotency_records")
    op.execute("DROP INDEX persona_minimal.generations_owner_active_idx")
    op.execute("DROP INDEX persona_minimal.generations_user_message_created_idx")
    op.execute("DROP INDEX persona_minimal.generations_conversation_created_idx")
    op.execute("DROP TABLE persona_minimal.generations")
    op.execute("DROP INDEX persona_minimal.user_messages_conversation_created_idx")
    op.execute("DROP TABLE persona_minimal.user_messages")
    op.execute("DROP INDEX persona_minimal.conversations_persona_owner_created_idx")
    op.execute("DROP TABLE persona_minimal.conversations")


def _downgrade_version_as_first_class_key() -> None:
    """0002의 "캐릭터당 version 한 행" 스키마로 되돌린다.

    데이터가 이미 그 모양을 벗어났으면(캐릭터 하나에 version 둘 이상) 되돌릴 수
    없다 — 어느 version을 남길지는 migration이 정할 문제가 아니다. PK 복구가
    중복 키로 실패하게 두면 원인이 "material_versions_pkey 중복"으로만 보이므로,
    먼저 읽을 수 있는 메시지로 끊는다.
    """
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM persona_minimal.material_versions
                GROUP BY persona_id HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    '0004 downgrade 불가: 캐릭터당 material_versions가 둘 이상이다. '
                    '0002 스키마는 캐릭터당 한 행만 담을 수 있으므로, 남길 version을 '
                    '정해 나머지를 먼저 정리해야 한다.';
            END IF;
        END
        $$
        """
    )

    op.execute(
        "ALTER TABLE persona_minimal.personas DROP CONSTRAINT personas_draft_version_id_fkey"
    )
    op.execute(
        "ALTER TABLE persona_minimal.personas DROP CONSTRAINT personas_active_version_id_fkey"
    )
    op.execute("ALTER TABLE persona_minimal.personas DROP COLUMN draft_version_id")
    op.execute("ALTER TABLE persona_minimal.personas DROP COLUMN active_version_id")
    op.execute("ALTER TABLE persona_minimal.idempotency_records DROP COLUMN version_id")

    # material_sources의 persona_id를 version에서 되살린다.
    op.execute("ALTER TABLE persona_minimal.material_sources ADD COLUMN persona_id uuid")
    op.execute(
        """
        UPDATE persona_minimal.material_sources AS s
        SET persona_id = v.persona_id
        FROM persona_minimal.material_versions AS v
        WHERE v.version_id = s.version_id
        """
    )
    op.execute("ALTER TABLE persona_minimal.material_sources ALTER COLUMN persona_id SET NOT NULL")
    op.execute("DROP INDEX persona_minimal.material_sources_version_kind_idx")
    op.execute(
        "ALTER TABLE persona_minimal.material_sources "
        "DROP CONSTRAINT material_sources_version_id_fkey"
    )
    op.execute("ALTER TABLE persona_minimal.material_sources DROP COLUMN version_id")

    op.execute("DROP INDEX persona_minimal.material_versions_persona_created_idx")
    op.execute(
        "ALTER TABLE persona_minimal.material_versions DROP CONSTRAINT material_versions_pkey"
    )
    op.execute(
        "ALTER TABLE persona_minimal.material_versions "
        "ADD CONSTRAINT material_versions_pkey PRIMARY KEY (persona_id)"
    )
    op.execute(
        "ALTER TABLE persona_minimal.material_versions "
        "ADD CONSTRAINT material_versions_version_id_key UNIQUE (version_id)"
    )
    op.execute(
        "ALTER TABLE persona_minimal.material_sources "
        "ADD CONSTRAINT material_sources_persona_id_fkey "
        "FOREIGN KEY (persona_id) REFERENCES persona_minimal.material_versions(persona_id)"
    )
    op.execute(
        "CREATE INDEX material_sources_persona_kind_idx "
        "ON persona_minimal.material_sources (persona_id, kind, created_at)"
    )
