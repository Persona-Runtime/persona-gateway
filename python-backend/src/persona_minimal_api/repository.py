from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from pgvector.psycopg import register_vector
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg import Error as PsycopgError
from psycopg_pool import ConnectionPool
from psycopg_pool import PoolTimeout

from .cursor import PersonaCursor
from .indexing.chunker import CHUNKABLE_KINDS

logger = logging.getLogger(__name__)

MAX_PERSONAS_PER_USER = 3
CREATE_PERSONA_OPERATION = "create_persona"
CREATE_PERSONA_SCOPE = "/v1/personas"
# 초안 생성과 폐기는 서로 다른 operation이다. 같은 키로 둘을 보내도 서로의 기록을 덮지 않는다.
CREATE_DRAFT_OPERATION = "create_draft"
DISCARD_DRAFT_OPERATION = "discard_draft"
# §4-6 구현(2026-09-18). 옛 14번 상한(파일 1 MiB·전체 5 MiB, 바이트 기준)을 대체한다.
# 코드포인트(len()) 기준 — UTF-8 바이트가 아니다. MAX_PROFILE_CHARS와 같은 이유로,
# 한글은 바이트 상한에서 훨씬 적은 글자 수만 허용돼 나무위키 긴 문서를 못 받는다.
MAX_BODY_KIND_CHARS = 200_000  # events/relationships/abilities 각각
MAX_SPEECH_CHARS = 100_000  # speech_examples 소스 전체
MAX_SPEECH_LINE_CHARS = 500  # speech_examples 한 줄
MAX_DRAFT_TOTAL_CHARS = 500_000  # 소스 전체 합(profile 제외)
# profile은 항상 프롬프트 블록 2에 전문이 들어가고 그 블록의 예산이 2,000자(시스템 지시
# 포함)라, 상한을 그 예산 안에 여유 있게 들어가는 값으로 낮춰 잘림 없이 항상 전문이
# 프롬프트에 들어가게 한다.
MAX_PROFILE_CHARS = 1500
DRAFT_KINDS = ("profile", "events", "relationships", "abilities", "speech_examples")
# readiness가 받아들이는 migration revision.
#
# 단일값 완전 일치를 요구하면 migration을 적용하는 순간 아직 이전 revision을 기대하는
# 파드가 스스로 Ready를 잃는다. Deployment가 replicas: 1이라 그 즉시 Service의 ready
# endpoint가 0이 되고, maxUnavailable: 0은 이 경우를 막지 못한다 — 컨트롤러가 파드를
# 내려서 생기는 공백이 아니기 때문이다.
#
# 목록을 넓히는 것은 호환 릴리스의 역할이지 기본값이 아니다. 새 migration을 배포할 때는
# 구·신 revision을 함께 허용하는 호환 릴리스를 먼저 내보내 공백을 없앤다.
# 배포 순서와 롤백 규칙은 docs/migrations.md.
#
# 호환 창을 닫았다(2026-09-21) — migration 0002·0003이 운영에 실제로 적용되고
# 복원 리허설까지 통과해(persona-platform/runbooks/gate3-4-apply-record-
# 2026-09-19.md §2-15) 0001·0002는 더 이상 지원하지 않는다. 2026-09-19 호환
# 릴리스가 잠깐 0001까지 넓혔던 것을 원래대로 되돌린 것 — 다음 migration(0004)을
# 낼 때 같은 패턴(호환 릴리스로 구·신 둘 다 잠깐 허용했다가, 적용 확인 후 새
# revision 하나로 좁히기)을 또 쓴다.
# 2026-09-22 호환 릴리스: 0004(채팅 테이블)를 새로 낸다. docs/migrations.md의 배포
# 순서(호환 릴리스 → migration Job → grants → 기능 릴리스)대로, 이 이미지는 새 기능
# 코드(chat/*)가 이미 있지만 0003 DB에서도 계속 Ready이게 구·신 둘 다 허용한다.
# migration Job이 실제로 적용되고 확인된 뒤 ("0004_chat",) 하나로 좁히는 건 이후
# 별도 커밋(기능 릴리스)이다 — 이 PR이 아니다.
SUPPORTED_ALEMBIC_REVISIONS = ("0003_material_chunks", "0004_chat")
# 초안(material_versions·material_sources 등) 테이블은 0002에서 생겼다. 호환 창을
# 다시 열면서(위 SUPPORTED_ALEMBIC_REVISIONS가 0003·0004 둘 다 허용) 이 목록도
# "0003까지는 있다"는 뜻을 유지하도록 그대로 둔다 — 0004 DB에도 초안 스키마는
# 당연히 있다(0002에서 만들어진 뒤 한 번도 지워지지 않았다).
DRAFT_SCHEMA_REVISIONS = ("0002_persona_draft", "0003_material_chunks", "0004_chat")
# 채팅 스키마(conversations·user_messages·generations)는 0004에서 생겼다. 호환 창이
# 열려 있는 동안(SUPPORTED_ALEMBIC_REVISIONS가 0003도 허용) 0003 DB엔 이 테이블들이
# 없을 수 있다 — chat_schema_ready/require_chat_schema가 draft_schema_ready와 같은
# "호환 창 도구" 패턴으로 이걸 가른다.
CHAT_SCHEMA_REVISIONS = ("0004_chat",)


def draft_schema_ready(cur) -> bool:
    """alembic_version 마커만 읽어 초안 스키마(material_versions 등) 존재 여부를 판정한다.

    is_ready()의 to_regclass 방식(테이블 실재를 직접 확인)과 다르게 마커만 본다 — 가볍다.
    이 함수 자체는 판정만 하고 무엇을 할지는 정하지 않는다: require_draft_schema는 이
    결과가 False면 거절하고, list_personas·get_persona·lifespan 시작 훅은 대신 초안
    관련 조회·정리를 건너뛴다.

    이건 "호환 창 도구"다 — SUPPORTED_ALEMBIC_REVISIONS가 여러 revision을 허용하는
    호환 릴리스 기간에만 이 판정이 실제로 갈린다(그중 일부는 초안 스키마가 없을 수
    있으므로). 지금(2026-09-21, 호환 창을 닫은 뒤)은 SUPPORTED_ALEMBIC_REVISIONS가
    "0003_material_chunks" 하나뿐이라 이 함수가 항상 True를 돌려주는 죽은 분기지만,
    지우지 않는다 — 다음 호환 릴리스(예: 0004)에서 그대로 재사용한다. 호출자는
    dict_row cursor를 넘겨야 한다.
    """
    cur.execute("SELECT version_num FROM persona_minimal.alembic_version")
    row = cur.fetchone()
    version = row["version_num"] if row else None
    return version in DRAFT_SCHEMA_REVISIONS


def require_draft_schema(cur) -> None:
    """draft_schema_ready가 False면 SchemaNotReady를 던진다.

    호출자가 뒤이어 material_versions 등을 어차피 SELECT/INSERT할 것이므로, 여기서는
    "구 revision인데 초안 API를 불렀다"는 사용자 친화적인 409를 먼저 내는 것이 목적이다.
    draft_schema_ready와 같은 이유로 "호환 창 도구"다 — 지금은 호출돼도 항상 통과만
    하지만 다음 호환 릴리스에서 다시 실제로 거절하게 된다. 호출자는 dict_row cursor를
    넘겨야 한다.
    """
    if not draft_schema_ready(cur):
        raise SchemaNotReady


def chat_schema_ready(cur) -> bool:
    """alembic_version 마커만 읽어 채팅 스키마(conversations 등) 존재 여부를 판정한다.

    draft_schema_ready와 같은 이유·같은 방식의 "호환 창 도구"다 — 0004 호환 릴리스
    기간에만 실제로 갈린다. 호출자는 dict_row cursor를 넘겨야 한다.
    """
    cur.execute("SELECT version_num FROM persona_minimal.alembic_version")
    row = cur.fetchone()
    version = row["version_num"] if row else None
    return version in CHAT_SCHEMA_REVISIONS


def require_chat_schema(cur) -> None:
    """chat_schema_ready가 False면 SchemaNotReady를 던진다.

    require_draft_schema와 같은 이유 — 호출자가 뒤이어 conversations 등을 SELECT/
    INSERT할 것이므로, "구 revision인데 채팅 API를 불렀다"는 409를 먼저 낸다.
    """
    if not chat_schema_ready(cur):
        raise SchemaNotReady


class SafePoolLogFilter(logging.Filter):
    """psycopg pool의 원문 연결 오류가 운영 로그에 남지 않게 한다."""

    def filter(self, record: logging.LogRecord) -> bool:
        # 진단 중 DEBUG/INFO를 켜도 연결 주소·사용자명·예외 원문이 로그에 남지 않게
        # 저수준 pool 로그는 통째로 버린다. 안전한 별도 진단 이벤트가 필요하면 원문을
        # 전달하지 않는 전용 메트릭 또는 로그로 추가해야 한다.
        if record.levelno < logging.WARNING:
            return False
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


class PersonaNotFound(Exception):
    """소유자가 다르거나 없는 캐릭터. 둘을 구분해 알리지 않는다."""


class DraftNotFound(Exception):
    """캐릭터는 있는데 초안이 없다."""


class DraftAlreadyExists(Exception):
    """초안은 캐릭터당 하나다(계약 2절)."""


class RevisionConflict(Exception):
    """expected_revision이 현재 초안과 다르다. 다른 사람의 수정을 덮지 않는다."""


class IndexingInProgress(Exception):
    """이미 색인이 진행 중이다(advisory lock을 못 잡았거나 status가 processing).

    같은 캐릭터에 색인 요청 두 개가 동시에 material_chunks를 지웠다 쓰면 서로
    덮어써 결과가 뒤섞인다. 먼저 잡은 쪽만 진행하고 나머지는 409로 돌려보낸다.
    """


class NoSourcesToIndex(Exception):
    """청킹 대상 소스가 하나도 없다. run_indexing이 결국 no_content로 실패할 것을
    미리 안다면 202→failed 왕복 없이 바로 422로 끝낸다."""


class NotIndexed(Exception):
    """캐릭터·초안은 있지만 성공한 색인이 한 번도 없다(indexed_revision IS NULL).
    검색할 대상 자체가 없다."""


class NotActivatable(Exception):
    """초안은 있지만 지금 활성화할 수 없다(Draft.can_activate가 거짓).

    색인이 끝나지 않았거나(status != 'ready'), 색인 뒤 자료를 더 고쳐
    indexed_revision이 현재 revision보다 뒤처진 경우다. 후자를 막지 않으면
    방금 고친 내용이 빠진 색인이 적용본이 된다."""


class NoActiveVersion(Exception):
    """적용본(personas.active_version_id)이 없다.

    대화는 적용본 위에서만 시작한다(계약 §7 "새 대화는 적용본이 있어야 한다").
    색인만 끝나고 아직 활성화하지 않은 상태와 색인 자체가 없는 상태를 구분하려고
    NotIndexed와 따로 둔다 — 화면이 "활성화하세요"와 "자료를 넣으세요"를 다르게
    안내할 수 있어야 한다."""


class SchemaNotReady(Exception):
    """migration이 아직 초안 스키마(material_versions 등)를 만들지 않았다(0001).

    2026-09-19 호환 릴리스 동안 SUPPORTED_ALEMBIC_REVISIONS는 0001도 허용해 readyz는
    통과하지만, 초안 관련 엔드포인트는 실제 테이블이 없어 500 대신 이 예외로 먼저
    끊는다. list_personas·get_persona는 이 검사를 받지 않는다 — 두 조회는 0001에서도
    정상 응답해야 한다(docs/migrations.md 배포 순서 참고).
    """


class DraftValidationError(Exception):
    """계약이 정한 거절. code가 그대로 응답의 오류 코드가 된다.

    fields는 openapi.json Error.fields 스키마({field, code} 둘 다 필수)를 그대로 따른다.
    원문은 절대 담지 않는다 — field가 어느 kind인지, code가 어느 상한을 어겼는지만 담는다.
    """

    def __init__(self, code: str, status: int = 422, fields: list[dict[str, str]] | None = None):
        self.code = code
        self.status = status
        self.fields = fields
        super().__init__(code)


@dataclass(frozen=True)
class DraftSummary:
    """목록·상세가 싣는 초안 요약. 본문은 담지 않는다."""

    version_id: UUID
    revision: int
    status: str
    job_id: UUID | None
    # requires_processing을 목록에서도 상세와 같은 규칙으로 계산하려면 이 값이 필요하다.
    # 예전에는 목록이 상수 True를 싣고 상세만 계산해, 같은 초안이 화면마다 다르게
    # 보일 수 있었다.
    indexed_revision: int | None = None

    @property
    def requires_processing(self) -> bool:
        """Draft.requires_processing과 같은 규칙(그쪽 docstring 참고)."""
        return not (self.status == "ready" and self.indexed_revision == self.revision)


@dataclass(frozen=True)
class Persona:
    id: UUID
    name: str
    created_at: datetime
    deletion_id: UUID | None
    deleted_at: datetime | None
    active_version_id: UUID | None = None
    draft: DraftSummary | None = None

    @property
    def status(self) -> str:
        """계약 2절이 정한 계산 규칙 그대로다.

        deleting이 우선이고, **적용본이 있으면 초안 처리 여부와 무관하게 ready**다.
        적용본이 없으면 초안 없음=needs_material, 실행 중=preparing, 그 외 초안 존재=review_required.

        저장 전용 경로로 만든 초안은 job이 없으므로 preparing이 되지 않는다.
        """
        if self.deletion_id is not None and self.deleted_at is None:
            return "deleting"
        if self.active_version_id is not None:
            return "ready"
        if self.draft is None:
            return "needs_material"
        if self.draft.job_id is not None and self.draft.status == "processing":
            return "preparing"
        return "review_required"


@dataclass(frozen=True)
class DraftSettings:
    """계약의 Settings. profile은 비어 있으면 안 된다(5절)."""

    name: str
    profile: str
    speech_examples: str


@dataclass(frozen=True)
class DraftSource:
    id: UUID
    kind: str
    filename: str | None
    content: str
    byte_size: int
    sha256: str


@dataclass(frozen=True)
class Draft:
    persona_id: UUID
    version_id: UUID
    revision: int
    status: str
    job_id: UUID | None
    base_version_id: UUID | None
    settings: DraftSettings
    sources: tuple[DraftSource, ...]
    updated_at: datetime
    # 실제로 검색에 쓸 수 있는 색인이 어느 revision 것인지. status(마지막 적용 시도
    # 결과)와 분리해 둔다 — rev 3 색인 성공 후 rev 4 편집·색인 실패에서도 rev 3 조각은
    # 그대로 쓸 수 있어야 하는데, status만 보면 failed라 그 사실을 알 수 없다.
    indexed_revision: int | None
    indexed_at: datetime | None
    # status='failed'일 때만 의미가 있다. 그 외에는 NULL(다음 색인 성공 시에도 NULL로
    # 되돌아간다) — migration 0003의 컬럼 주석과 같은 규칙이다.
    error_code: str | None

    @property
    def can_activate(self) -> bool:
        """지금 이 초안을 적용본으로 활성화할 수 있는지(서버 판정값, 계약 §2).

        조건은 두 개가 동시에 참일 때다 — 마지막 적용 시도가 성공했고(status='ready'),
        그 성공한 색인이 **지금 내용의** 색인이어야 한다(indexed_revision == revision).
        둘을 따로 보는 이유: 색인 성공 뒤 자료를 더 고치면 revision만 올라가고
        indexed_revision은 그대로라, status만 보면 "성공했으니 적용 가능"으로 잘못
        읽힌다. 그 상태로 활성화하면 사용자가 방금 고친 내용이 빠진 색인이 적용본이 된다.
        """
        return self.status == "ready" and self.indexed_revision == self.revision

    @property
    def requires_processing(self) -> bool:
        """색인을 (다시) 돌려야 하는지. can_activate의 반대다.

        편집 직후(revision만 올라감)·실패·진행 중이 전부 여기 해당한다 — 어느 쪽이든
        지금 내용으로 색인이 끝나 있지 않다는 뜻은 같다.
        """
        return not self.can_activate


@dataclass(frozen=True)
class ActivatedVersion:
    """activate_draft의 결과. 계약의 Activated 응답 그대로다."""

    persona_id: UUID
    version_id: UUID
    activated_at: datetime


@dataclass(frozen=True)
class IndexingHandle:
    """`start_indexing`이 만들고 `runner.run_indexing`이 소비하는 진행 중 색인 상태.

    `connection`은 advisory lock을 쥔 **바로 그** 연결이다. advisory lock은 세션(연결)
    범위라, 이 연결을 pool에 반납했다가 다시 꺼내면 다른 요청이 그 물리 연결을 받아
    영문도 모른 채 잠금을 쥔 것처럼 보일 수 있다 — 그래서 `run_indexing`이 끝날 때까지
    이 연결은 `pool.getconn()`으로 꺼낸 채 유지하고, 풀의 `with pool.connection()`
    컨텍스트 매니저(진입 시 획득·종료 시 자동 반납)를 쓰지 않는다.
    """

    connection: Connection
    pool: ConnectionPool
    persona_id: UUID
    version_id: UUID
    revision: int


class PersonaStore(Protocol):
    def list_personas(
        self, owner_subject: str, limit: int, cursor: PersonaCursor | None
    ) -> list[Persona]: ...

    def create_persona(
        self, owner_subject: str, display_name: str, name: str, idempotency_key: UUID
    ) -> Persona: ...

    def get_persona(self, owner_subject: str, persona_id: UUID) -> Persona: ...

    def create_draft(
        self,
        owner_subject: str,
        persona_id: UUID,
        settings: DraftSettings,
        idempotency_key: UUID,
    ) -> Draft: ...

    def get_draft(self, owner_subject: str, persona_id: UUID) -> Draft: ...

    def patch_draft(
        self,
        owner_subject: str,
        persona_id: UUID,
        expected_revision: int,
        settings: dict[str, str] | None,
        upsert_sources: list[dict[str, object]],
        remove_source_ids: list[UUID],
    ) -> Draft: ...

    def discard_draft(
        self, owner_subject: str, persona_id: UUID, idempotency_key: UUID
    ) -> None: ...

    def start_indexing(
        self, owner_subject: str, persona_id: UUID, expected_revision: int
    ) -> IndexingHandle: ...

    def activate_draft(
        self, owner_subject: str, persona_id: UUID, expected_revision: int
    ) -> ActivatedVersion: ...


@runtime_checkable
class ReadinessStore(Protocol):
    def is_ready(self) -> bool: ...


def fingerprint_name(name: str) -> bytes:
    return hashlib.sha256(name.encode("utf-8")).digest()


def _persona(row: dict[str, object]) -> Persona:
    version_id = row.get("draft_version_id")
    return Persona(
        id=row["id"],  # type: ignore[arg-type]
        name=row["name"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        deletion_id=row["deletion_id"],  # type: ignore[arg-type]
        deleted_at=row["deleted_at"],  # type: ignore[arg-type]
        # 0001~0003 시절 SELECT에는 이 컬럼이 없다. 호환 창(0003·0004 둘 다 허용)
        # 동안에는 0003 DB에서도 이 함수가 불리므로 get()으로 읽는다.
        active_version_id=row.get("active_version_id"),  # type: ignore[arg-type]
        draft=(
            DraftSummary(
                version_id=version_id,  # type: ignore[arg-type]
                revision=row["draft_revision"],  # type: ignore[arg-type]
                status=row["draft_status"],  # type: ignore[arg-type]
                job_id=row["draft_job_id"],  # type: ignore[arg-type]
                indexed_revision=row.get("draft_indexed_revision"),  # type: ignore[arg-type]
            )
            if version_id is not None
            else None
        ),
    )


def fingerprint_settings(settings: DraftSettings) -> bytes:
    """초안 생성 요청의 지문. 같은 키에 다른 설정이 오면 409로 가른다."""
    joined = "\x00".join((settings.name, settings.profile, settings.speech_examples))
    return hashlib.sha256(joined.encode("utf-8")).digest()


def draft_scope(persona_id: UUID) -> str:
    """초안 멱등 기록의 target_scope. persona_id를 값으로 넣는다.

    기록의 PK에 persona_id가 없으므로, 고정 문자열로 두면 한 사용자가 같은 키로
    다른 캐릭터에 초안을 만들 때 두 요청이 같은 행을 두고 부딪친다.
    """
    return f"/v1/personas/{persona_id}/draft"


class PostgresPersonaStore:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def list_personas(
        self, owner_subject: str, limit: int, cursor: PersonaCursor | None
    ) -> list[Persona]:
        # 초안 요약만 join한다. 본문(content)은 여기서 절대 읽지 않는다 — 목록 한 번에
        # 캐릭터 수만큼의 원문을 실어 나르게 되고, 화면은 그중 아무것도 쓰지 않는다.
        values: list[object] = [owner_subject]
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cur:
                if draft_schema_ready(cur):
                    # active_version_id는 0004에서 생겼다. 호환 창 동안 0003 DB에서도
                    # 이 목록은 200이어야 하므로, 컬럼이 없는 revision에서는 상수
                    # NULL로 대신한다(컬럼을 그냥 쓰면 UndefinedColumn으로 죽는다).
                    active_column = (
                        "p.active_version_id"
                        if chat_schema_ready(cur)
                        else "NULL AS active_version_id"
                    )
                    query = f"""
                        SELECT p.id, p.name, p.created_at, p.deletion_id, p.deleted_at,
                               {active_column},
                               d.version_id AS draft_version_id, d.revision AS draft_revision,
                               d.status AS draft_status, d.job_id AS draft_job_id,
                               d.indexed_revision AS draft_indexed_revision
                        FROM persona_minimal.personas AS p
                        LEFT JOIN persona_minimal.material_versions AS d ON d.persona_id = p.id
                        WHERE p.owner_subject = %s AND p.deleted_at IS NULL
                    """
                else:
                    # 호환 창 도구 — 구 revision(호환 릴리스가 허용하는 동안)에는
                    # material_versions가 물리적으로 없다. LEFT JOIN도 참조 테이블이
                    # 없으면 PostgreSQL이 그 자리에서 예외를 던진다(join 종류와 무관 —
                    # null 처리로 넘어가는 문제가 아니다) — 조인 자체를 빼고 캐릭터만
                    # 돌려준다. list_personas·get_persona는 구 revision에서도 200이어야
                    # 한다는 요구가 있어 거절할 수 없다(require_draft_schema와 다른
                    # 이유로 draft_schema_ready를 쓴다). 지금은 SUPPORTED_ALEMBIC_
                    # REVISIONS가 단일값이라 draft_schema_ready가 항상 True를 돌려줘
                    # 이 분기가 실행되지 않지만, 다음 호환 릴리스에서 재사용한다.
                    query = """
                        SELECT p.id, p.name, p.created_at, p.deletion_id, p.deleted_at,
                               NULL AS draft_version_id, NULL AS draft_revision,
                               NULL AS draft_status, NULL AS draft_job_id,
                               NULL AS draft_indexed_revision
                        FROM persona_minimal.personas AS p
                        WHERE p.owner_subject = %s AND p.deleted_at IS NULL
                    """
                if cursor is not None:
                    query += " AND (p.created_at, p.id) < (%s, %s)"
                    values.extend([cursor.created_at, cursor.persona_id])
                query += " ORDER BY p.created_at DESC, p.id DESC LIMIT %s"
                values.append(limit)
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

    # --- 캐릭터 상세와 초안 ------------------------------------------------

    # {active} 자리에는 p.active_version_id(0004 이상) 또는 NULL 상수(0003, 호환 창)가
    # 들어간다 — list_personas와 같은 이유다.
    _PERSONA_WITH_DRAFT = """
        SELECT p.id, p.name, p.created_at, p.deletion_id, p.deleted_at,
               {active},
               d.version_id AS draft_version_id, d.revision AS draft_revision,
               d.status AS draft_status, d.job_id AS draft_job_id,
               d.indexed_revision AS draft_indexed_revision
        FROM persona_minimal.personas AS p
        LEFT JOIN persona_minimal.material_versions AS d ON d.persona_id = p.id
        WHERE p.id = %s AND p.owner_subject = %s AND p.deleted_at IS NULL
    """

    # 호환 창 도구(list_personas와 같은 이유) — material_versions가 물리적으로 없는
    # 구 revision에서 쓴다. 지금은 draft_schema_ready가 항상 True라 실행되지 않지만,
    # 다음 호환 릴리스에서 재사용한다.
    _PERSONA_WITHOUT_DRAFT_SCHEMA = """
        SELECT p.id, p.name, p.created_at, p.deletion_id, p.deleted_at,
               NULL AS draft_version_id, NULL AS draft_revision,
               NULL AS draft_status, NULL AS draft_job_id,
               NULL AS draft_indexed_revision
        FROM persona_minimal.personas AS p
        WHERE p.id = %s AND p.owner_subject = %s AND p.deleted_at IS NULL
    """

    def get_persona(self, owner_subject: str, persona_id: UUID) -> Persona:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cur:
                if draft_schema_ready(cur):
                    query = self._PERSONA_WITH_DRAFT.format(
                        active=(
                            "p.active_version_id"
                            if chat_schema_ready(cur)
                            else "NULL AS active_version_id"
                        )
                    )
                else:
                    query = self._PERSONA_WITHOUT_DRAFT_SCHEMA
                cur.execute(query, (persona_id, owner_subject))
                row = cur.fetchone()
                if row is None:
                    raise PersonaNotFound
                return _persona(row)

    def _lock_persona(self, cur, owner_subject: str, persona_id: UUID) -> None:
        """소유자 범위로 캐릭터를 잠근다.

        타인 소유와 부재를 같은 PersonaNotFound로 올린다. 둘을 구분해 알리면
        남의 캐릭터가 존재하는지가 새어 나간다.

        FOR UPDATE로 잠그는 이유: 초안을 만드는 동안 같은 캐릭터에 다른 요청이 들어오면
        둘 다 "초안 없음"을 보고 각자 만들려 한다. PK가 막아 주지만 오류가 아니라
        409로 답해야 하므로 여기서 직렬화한다.
        """
        cur.execute(
            """
            SELECT id FROM persona_minimal.personas
            WHERE id = %s AND owner_subject = %s AND deleted_at IS NULL
            FOR UPDATE
            """,
            (persona_id, owner_subject),
        )
        if cur.fetchone() is None:
            raise PersonaNotFound

    def _read_draft(self, cur, persona_id: UUID) -> Draft:
        cur.execute(
            """
            SELECT persona_id, version_id, revision, status, job_id, base_version_id,
                   settings_name, settings_profile, settings_speech_examples, updated_at,
                   indexed_revision, indexed_at, error_code
            FROM persona_minimal.material_versions WHERE persona_id = %s
            """,
            (persona_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise DraftNotFound
        cur.execute(
            """
            SELECT id, kind, filename, content, byte_size, sha256
            FROM persona_minimal.material_sources
            WHERE persona_id = %s ORDER BY kind, created_at, id
            """,
            (persona_id,),
        )
        sources = tuple(
            DraftSource(
                id=s["id"],
                kind=s["kind"],
                filename=s["filename"],
                content=s["content"],
                byte_size=s["byte_size"],
                sha256=s["sha256"],
            )
            for s in cur.fetchall()
        )
        return Draft(
            persona_id=row["persona_id"],
            version_id=row["version_id"],
            revision=row["revision"],
            status=row["status"],
            job_id=row["job_id"],
            base_version_id=row["base_version_id"],
            settings=DraftSettings(
                name=row["settings_name"],
                profile=row["settings_profile"],
                speech_examples=row["settings_speech_examples"],
            ),
            sources=sources,
            updated_at=row["updated_at"],
            indexed_revision=row["indexed_revision"],
            indexed_at=row["indexed_at"],
            error_code=row["error_code"],
        )

    def get_draft(self, owner_subject: str, persona_id: UUID) -> Draft:
        with self.pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cur:
                require_draft_schema(cur)
                cur.execute(
                    """
                    SELECT 1 FROM persona_minimal.personas
                    WHERE id = %s AND owner_subject = %s AND deleted_at IS NULL
                    """,
                    (persona_id, owner_subject),
                )
                if cur.fetchone() is None:
                    raise PersonaNotFound
                return self._read_draft(cur, persona_id)

    def create_draft(
        self,
        owner_subject: str,
        persona_id: UUID,
        settings: DraftSettings,
        idempotency_key: UUID,
    ) -> Draft:
        """처리 없이 초안을 시작한다. job을 만들지 않는다(계약 5절).

        멱등 기록은 캐릭터 생성과 같은 테이블·같은 순서를 쓴다 — 캐릭터 행을 잠근 뒤
        기록을 보고, 같은 키면 그때 만든 초안을 그대로 돌려준다.
        """
        fingerprint = fingerprint_settings(settings)
        with self.pool.connection() as connection:
            with connection.transaction():
                with connection.cursor(row_factory=dict_row) as cur:
                    require_draft_schema(cur)
                    self._lock_persona(cur, owner_subject, persona_id)
                    cur.execute(
                        """
                        SELECT request_fingerprint
                        FROM persona_minimal.idempotency_records
                        WHERE owner_subject = %s AND operation = %s AND target_scope = %s
                          AND idempotency_key = %s
                        """,
                        (
                            owner_subject,
                            CREATE_DRAFT_OPERATION,
                            draft_scope(persona_id),
                            idempotency_key,
                        ),
                    )
                    record = cur.fetchone()
                    if record is not None:
                        # 이미 성공한 같은 키다. 초안이 이미 있다는 이유로 409를 내면
                        # 응답이 유실된 요청의 재전송이 실패로 보인다.
                        if bytes(record["request_fingerprint"]) != fingerprint:
                            raise IdempotencyConflict
                        return self._read_draft(cur, persona_id)

                    cur.execute(
                        "SELECT 1 FROM persona_minimal.material_versions WHERE persona_id = %s",
                        (persona_id,),
                    )
                    if cur.fetchone() is not None:
                        raise DraftAlreadyExists

                    self._guard_draft_limits(settings, ())

                    cur.execute(
                        """
                        INSERT INTO persona_minimal.material_versions
                            (persona_id, version_id, revision, status,
                             settings_name, settings_profile, settings_speech_examples)
                        VALUES (%s, %s, 1, 'editing', %s, %s, %s)
                        """,
                        (
                            persona_id,
                            uuid4(),
                            settings.name,
                            settings.profile,
                            settings.speech_examples,
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO persona_minimal.idempotency_records(
                            owner_subject, operation, target_scope, idempotency_key,
                            request_fingerprint, persona_id
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            owner_subject,
                            CREATE_DRAFT_OPERATION,
                            draft_scope(persona_id),
                            idempotency_key,
                            fingerprint,
                            persona_id,
                        ),
                    )
                    return self._read_draft(cur, persona_id)

    def discard_draft(self, owner_subject: str, persona_id: UUID, idempotency_key: UUID) -> None:
        """초안과 그 자료를 지운다. 캐릭터는 남는다."""
        with self.pool.connection() as connection:
            with connection.transaction():
                with connection.cursor(row_factory=dict_row) as cur:
                    self._lock_persona(cur, owner_subject, persona_id)
                    cur.execute(
                        """
                        SELECT 1 FROM persona_minimal.idempotency_records
                        WHERE owner_subject = %s AND operation = %s AND target_scope = %s
                          AND idempotency_key = %s
                        """,
                        (
                            owner_subject,
                            DISCARD_DRAFT_OPERATION,
                            draft_scope(persona_id),
                            idempotency_key,
                        ),
                    )
                    if cur.fetchone() is not None:
                        # 응답이 유실된 폐기 요청의 재전송이다. 이미 없는 초안을 다시 찾아
                        # 404를 내면 요청자는 실패한 줄 알고 되돌리려 한다.
                        return

                    # 자료를 먼저 지운다. FK가 초안을 가리키고 있다.
                    cur.execute(
                        "DELETE FROM persona_minimal.material_sources WHERE persona_id = %s",
                        (persona_id,),
                    )
                    cur.execute(
                        "DELETE FROM persona_minimal.material_versions WHERE persona_id = %s",
                        (persona_id,),
                    )
                    if cur.rowcount == 0:
                        # 실패한 요청의 키는 비워 둬야 같은 키로 다시 시도할 수 있다.
                        raise DraftNotFound
                    cur.execute(
                        """
                        INSERT INTO persona_minimal.idempotency_records(
                            owner_subject, operation, target_scope, idempotency_key,
                            request_fingerprint, persona_id
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            owner_subject,
                            DISCARD_DRAFT_OPERATION,
                            draft_scope(persona_id),
                            idempotency_key,
                            hashlib.sha256(b"discard_draft").digest(),
                            persona_id,
                        ),
                    )

    def patch_draft(
        self,
        owner_subject: str,
        persona_id: UUID,
        expected_revision: int,
        settings: dict[str, str] | None,
        upsert_sources: list[dict[str, object]],
        remove_source_ids: list[UUID],
    ) -> Draft:
        """초안을 고친다. revision CAS로 남의 수정을 덮지 않는다.

        검사와 반영을 **한 트랜잭션**에서 한다. 나눠 두면 검사를 통과한 두 요청이
        각각 revision을 올려, 나중에 커밋한 쪽이 앞선 수정을 조용히 지운다.
        """
        with self.pool.connection() as connection:
            with connection.transaction():
                with connection.cursor(row_factory=dict_row) as cur:
                    require_draft_schema(cur)
                    self._lock_persona(cur, owner_subject, persona_id)
                    # 초안 행까지 잠근다. 위의 캐릭터 잠금만으로는 같은 캐릭터의
                    # 동시 PATCH가 같은 revision을 읽는 것을 막지 못한다.
                    cur.execute(
                        "SELECT revision FROM persona_minimal.material_versions "
                        "WHERE persona_id = %s FOR UPDATE",
                        (persona_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise DraftNotFound
                    if row["revision"] != expected_revision:
                        raise RevisionConflict

                    current = self._read_draft(cur, persona_id)
                    self._apply_settings(cur, persona_id, current, settings, upsert_sources)
                    self._apply_sources(cur, persona_id, current, upsert_sources, remove_source_ids)

                    cur.execute(
                        """
                        UPDATE persona_minimal.material_versions
                        SET revision = revision + 1, status = 'editing', updated_at = now()
                        WHERE persona_id = %s
                        """,
                        (persona_id,),
                    )
                    updated = self._read_draft(cur, persona_id)
                    self._guard_draft_limits(updated.settings, updated.sources)
                    return updated

    def activate_draft(
        self, owner_subject: str, persona_id: UUID, expected_revision: int
    ) -> ActivatedVersion:
        """색인이 끝난 초안을 캐릭터의 적용본으로 세운다(계약 §6).

        순서: 캐릭터 잠금 → 초안 행 잠금 → revision CAS → can_activate 판정 →
        personas.active_version_id 전환. 잠금을 먼저 잡는 이유는 판정과 전환 사이에
        PATCH나 색인이 끼어들면 "판정할 때는 최신이었는데 적용할 때는 아닌" 상태가
        그대로 적용본이 되기 때문이다.

        **초안 슬롯을 비우지 않는다.** 계약 §6은 "성공 후 초안 슬롯은 비워 다음 수정을
        허용한다"고 적지만, 지금 스키마에서 그렇게 하면 적용본이 쓸 색인이 사라진다 —
        material_sources가 material_versions(persona_id)를 참조하고 material_chunks가
        material_sources(id)를 참조해, 초안 행을 지우려면 자료와 조각을 먼저 지워야
        한다. 계약이 전제하는 "immutable settings + immutable index reference 묶음"
        (§6)이 별도 테이블로 있어야 가능한 동작이고, 그 스키마는 아직 없다.
        그래서 지금은 포인터만 세우고 초안은 그대로 둔다 — 재적용은 같은 슬롯에서
        수정 → 색인 → activate로 반복한다.

        같은 이유로 active_version_id가 가리키는 version_id는 불변 스냅샷이 아니다:
        material_versions는 캐릭터당 한 행이고 PATCH는 revision만 올리므로 version_id가
        유지된다. 즉 지금의 적용본은 "이 캐릭터는 한 번 이상 활성화됐다"는 표시에
        가깝고, 재색인하면 같은 version_id 아래 조각이 바뀐다. 불변 묶음은 후속 과제다.
        """
        with self.pool.connection() as connection:
            with connection.transaction():
                with connection.cursor(row_factory=dict_row) as cur:
                    require_draft_schema(cur)
                    require_chat_schema(cur)
                    self._lock_persona(cur, owner_subject, persona_id)
                    cur.execute(
                        """
                        SELECT version_id, revision, status, indexed_revision
                        FROM persona_minimal.material_versions
                        WHERE persona_id = %s FOR UPDATE
                        """,
                        (persona_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise DraftNotFound
                    if row["revision"] != expected_revision:
                        raise RevisionConflict
                    # Draft.can_activate와 같은 규칙이다. 여기서 다시 판정하는 이유는
                    # 응답을 만들 때 읽은 값과 지금 잠근 행이 다를 수 있어서다.
                    if not (
                        row["status"] == "ready" and row["indexed_revision"] == row["revision"]
                    ):
                        raise NotActivatable
                    cur.execute(
                        """
                        UPDATE persona_minimal.personas
                        SET active_version_id = %s
                        WHERE id = %s
                        RETURNING active_version_id
                        """,
                        (row["version_id"], persona_id),
                    )
                    activated = cur.fetchone()
                    return ActivatedVersion(
                        persona_id=persona_id,
                        version_id=activated["active_version_id"],
                        activated_at=datetime.now(timezone.utc),
                    )

    def start_indexing(
        self, owner_subject: str, persona_id: UUID, expected_revision: int
    ) -> IndexingHandle:
        """색인을 시작할 수 있는지 확인하고, 시작한다면 그 연결을 쥔 채로 넘긴다.

        advisory lock은 이 메서드가 반환한 뒤에도 `run_indexing`이 끝낼 때까지
        유지돼야 하므로, 여기서는 `pool.connection()`(종료 시 자동 반납)이 아니라
        `pool.getconn()`을 쓴다. 실패하는 모든 경로에서 잠금을 풀고 연결을 반납한
        뒤 예외를 던진다 — 그래야 실패한 시도가 연결을 새어 나가게 하지 않는다.

        `status == 'processing'`일 때만 막는다(`IndexingInProgress`). ready·failed·
        editing에서는 재적용을 허용한다 — "이미 진행 중일 때만" 막으라는 계약 그대로다.
        """
        connection = self.pool.getconn()
        try:
            with connection.transaction():
                with connection.cursor(row_factory=dict_row) as cur:
                    # advisory lock을 잡기 전에 확인한다 — 여기서 실패하면 아직 아무
                    # 잠금도 없어 뒤따르는 unlock 없이 바로 예외를 던져도 된다.
                    require_draft_schema(cur)
                    self._lock_persona(cur, owner_subject, persona_id)

                    cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (str(persona_id),))
                    locked = cur.fetchone()["pg_try_advisory_lock"]
                    if not locked:
                        raise IndexingInProgress

                    cur.execute(
                        "SELECT version_id, revision, status FROM persona_minimal.material_versions "
                        "WHERE persona_id = %s FOR UPDATE",
                        (persona_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (str(persona_id),))
                        raise DraftNotFound
                    if row["status"] == "processing":
                        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (str(persona_id),))
                        raise IndexingInProgress
                    if row["revision"] != expected_revision:
                        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (str(persona_id),))
                        raise RevisionConflict

                    # 소스가 하나도 없으면 run_indexing이 결국 no_content로 실패할 게
                    # 뻔하다 — 202로 받아놓고 비동기로 실패시키는 왕복을 줄이려고 여기서
                    # 바로 거절한다.
                    cur.execute(
                        "SELECT EXISTS (SELECT 1 FROM persona_minimal.material_sources "
                        "WHERE persona_id = %s AND kind = ANY(%s))",
                        (persona_id, list(CHUNKABLE_KINDS)),
                    )
                    if not cur.fetchone()["exists"]:
                        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (str(persona_id),))
                        raise NoSourcesToIndex

                    revision = row["revision"]
                    version_id = row["version_id"]
                    cur.execute(
                        "UPDATE persona_minimal.material_versions SET status = 'processing' "
                        "WHERE persona_id = %s",
                        (persona_id,),
                    )
        except BaseException:
            # pg_try_advisory_lock 성공 뒤 FOR UPDATE/UPDATE에서 예상 밖 DB 오류가 나면
            # 잠금을 쥔 채 연결이 pool로 돌아가 이 캐릭터의 apply가 재시작 전까지 전부
            # 409(processing)에 갇힌다. unlock_all은 잠금이 없어도 무해하니 조건 없이
            # 부른다. 이 호출 자체가 실패해도 putconn은 반드시 실행한다 — 연결이 끊긴
            # 경우라면 서버가 세션 종료로 이미 잠금을 풀었고 pool도 그 연결을 버린다.
            try:
                connection.execute("SELECT pg_advisory_unlock_all()")
            except Exception:
                pass
            self.pool.putconn(connection)
            raise
        # 트랜잭션은 여기서 이미 커밋됐다(with connection.transaction() 블록 종료).
        # advisory lock은 세션 범위라 커밋 이후에도, 이 연결이 열려 있는 한 유지된다.
        return IndexingHandle(
            connection=connection,
            pool=self.pool,
            persona_id=persona_id,
            version_id=version_id,
            revision=revision,
        )

    def _apply_settings(
        self,
        cur,
        persona_id: UUID,
        current: Draft,
        settings: dict[str, str] | None,
        upsert_sources: list[dict[str, object]],
    ) -> None:
        if settings is None:
            return
        # 계약 5절: settings.profile과 profile source를 한 요청에서 함께 고치면 거부한다.
        # 원문과 설정을 서로 다른 두 진실로 만들지 않기 위한 규칙이다.
        touched = {str(item.get("kind")) for item in upsert_sources}
        for field in ("profile", "speech_examples"):
            if field in settings and field in touched:
                raise DraftValidationError("conflicting_fields")

        profile = settings.get("profile", current.settings.profile)
        if not profile.strip():
            # 5절: 최종 초안에서도 비공백 profile은 필수다.
            raise DraftValidationError("invalid_settings")
        # profile 길이는 여기서 확인하지 않는다 — patch_draft가 반영 뒤
        # _guard_draft_limits(updated.settings, updated.sources)로 한 번에 확인한다.
        name = settings.get("name", current.settings.name)
        if not name.strip():
            raise DraftValidationError("invalid_settings")

        cur.execute(
            """
            UPDATE persona_minimal.material_versions
            SET settings_name = %s, settings_profile = %s, settings_speech_examples = %s
            WHERE persona_id = %s
            """,
            (
                name,
                profile,
                settings.get("speech_examples", current.settings.speech_examples),
                persona_id,
            ),
        )

    def _apply_sources(
        self,
        cur,
        persona_id: UUID,
        current: Draft,
        upsert_sources: list[dict[str, object]],
        remove_source_ids: list[UUID],
    ) -> None:
        known = {source.id for source in current.sources}
        removing = set(remove_source_ids)
        # 제거 대상은 현재 초안의 것이어야 한다. 서버나 다른 초안의 id는 거부한다.
        if not removing <= known:
            raise DraftValidationError("unknown_source")

        for item in upsert_sources:
            source_id = item.get("id")
            if source_id is not None:
                if source_id not in known:
                    raise DraftValidationError("unknown_source")
                # 같은 id를 고치면서 동시에 지우라는 요청은 무엇을 원하는지 알 수 없다.
                if source_id in removing:
                    raise DraftValidationError("conflicting_fields")

        for source_id in remove_source_ids:
            cur.execute(
                "DELETE FROM persona_minimal.material_sources WHERE persona_id = %s AND id = %s",
                (persona_id, source_id),
            )

        for item in upsert_sources:
            kind = str(item["kind"])
            if kind not in DRAFT_KINDS:
                raise DraftValidationError("invalid_source_kind")
            content = str(item["content"])
            if not content.strip():
                raise DraftValidationError("invalid_source")
            # 길이 상한은 여기서 확인하지 않는다 — patch_draft가 반영 뒤
            # _guard_draft_limits로 kind별·합계 상한을 한 번에 확인한다.
            encoded = content.encode("utf-8")
            # 파일명은 표시용 basename이다. 경로 성분이 있으면 그대로 저장하지 않는다.
            filename = item.get("filename")
            if filename is not None:
                filename = str(filename).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            digest = hashlib.sha256(encoded).hexdigest()
            cur.execute(
                """
                INSERT INTO persona_minimal.material_sources
                    (id, persona_id, kind, filename, content, byte_size, sha256)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    filename = EXCLUDED.filename,
                    content = EXCLUDED.content,
                    byte_size = EXCLUDED.byte_size,
                    sha256 = EXCLUDED.sha256
                """,
                (
                    item.get("id") or uuid4(),
                    persona_id,
                    kind,
                    filename,
                    content,
                    len(encoded),
                    digest,
                ),
            )

    def _guard_draft_limits(
        self, settings: DraftSettings, sources: tuple[DraftSource, ...]
    ) -> None:
        """§4-6 글자 상한. create_draft(반영 전)·patch_draft(반영 후 재조회) 양쪽이 쓴다.

        각 자료를 넣기 전에 따로 세면 여러 건을 한 번에 보낸 요청이 합계를 넘길 수 있다 —
        patch_draft는 그래서 반영(INSERT/UPDATE) 뒤 실제 저장량으로 이 함수를 부른다.
        코드포인트(len()) 기준이다 — MAX_PROFILE_CHARS와 같은 이유로 UTF-8 바이트가 아니다.

        같은 kind로 소스가 여러 개일 수 있다(id 없이 upsert하면 새 소스가 생긴다 —
        여러 파일을 같은 kind로 나눠 넣는 경우). kind별 상한은 그 kind의 소스 **합**에
        적용한다 — 소스 하나씩 따로 보면 여러 개로 쪼개서 상한을 우회할 수 있다.
        """
        if len(settings.profile) > MAX_PROFILE_CHARS:
            raise DraftValidationError(
                "settings_too_large", fields=[{"field": "profile", "code": "max_length_exceeded"}]
            )

        by_kind: dict[str, list[DraftSource]] = {}
        for source in sources:
            by_kind.setdefault(source.kind, []).append(source)

        for kind in ("events", "relationships", "abilities"):
            kind_total = sum(len(source.content) for source in by_kind.get(kind, ()))
            if kind_total > MAX_BODY_KIND_CHARS:
                raise DraftValidationError(
                    "settings_too_large",
                    fields=[{"field": kind, "code": "max_length_exceeded"}],
                )

        speech_sources = by_kind.get("speech_examples", ())
        for source in speech_sources:
            for line in source.content.split("\n"):
                if len(line) > MAX_SPEECH_LINE_CHARS:
                    raise DraftValidationError(
                        "settings_too_large",
                        fields=[{"field": "speech_examples", "code": "line_too_long"}],
                    )
        speech_total = sum(len(source.content) for source in speech_sources)
        if speech_total > MAX_SPEECH_CHARS:
            raise DraftValidationError(
                "settings_too_large",
                fields=[{"field": "speech_examples", "code": "max_length_exceeded"}],
            )

        # kind="profile" 소스는 드물고(웹 클라이언트는 보내지 않는다) kind별 상한이
        # 정해져 있지 않다 — 옛 바이트 검사도 이 kind를 예외 취급하지 않았던 것과 같은
        # 이유로, 아래 합계 검사에만 포함시킨다.
        total = sum(len(source.content) for source in sources)
        if total > MAX_DRAFT_TOTAL_CHARS:
            raise DraftValidationError(
                "settings_too_large",
                fields=[{"field": "sources", "code": "max_total_length_exceeded"}],
            )

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
                                WHERE version_num = ANY(%s)
                            )
                        """,
                        (list(SUPPORTED_ALEMBIC_REVISIONS),),
                    )
                    row = cur.fetchone()
                    return row is not None and bool(row[0])
        except (PoolTimeout, PsycopgError):
            return False


def _remaining_seconds(deadline: float) -> float:
    return max(0.001, deadline - time.monotonic())


def _statement_timeout_value(deadline: float) -> str:
    return f"{max(1, int(_remaining_seconds(deadline) * 1000))}ms"


# 프로세스당 한 번만 경고하기 위한 플래그. 여러 워커 스레드가 거의 동시에 연결을
# 열면 드물게 중복 로그가 한두 번 더 찍힐 수 있다(락 없는 체크-후-설정) — 로그
# 중복 방지가 목적이라 그 정도 경합은 감수한다, 잠금까지 걸 정확성이 필요한
# 값이 아니다.
_pgvector_adapter_warning_logged = False


def _configure_connection(conn: Connection) -> None:
    """pgvector 타입 어댑터를 조건부로 등록한다.

    pgvector 타입 어댑터는 연결마다 따로 등록해야 한다(전역이 아니다). 등록해 두지
    않으면, material_chunks.embedding을 읽는 쪽이 register_vector를 몰라 vector를
    "[0.1,0.2,...]" 문자열로 받는다 — 등록한 쪽만 올바른 값을 본다.

    register_vector는 그 DB에 CREATE EXTENSION vector가 돼 있어야 성공한다. 확장은
    migration 0003에서만 만들어지므로, 0001 호환 릴리스가 허용하는 DB에는 없을 수
    있다 — 없는데 무조건 부르면 그 연결 자체가 실패해 pool이 새 연결을 계속 못 얻고
    PoolTimeout으로 번진다(실측 2026-09-21: 운영 CrashLoop, persona-platform/
    runbooks/gate3-4-apply-record-2026-09-19.md §2-14). try/except로 실패를 삼키지
    않는다 — 권한 부족 등 다른 이유의 실패는 그대로 드러나야 한다. 대신 존재 여부를
    직접 조회해 판정한다.

    0002·0003에서 이 어댑터가 필요한 코드(retrieval/search.py의 search(),
    indexing/store.py의 replace_chunks())는 전부 require_draft_schema 가드
    뒤에서만 실행된다 — 0001에서 이 어댑터를 건너뛰어도 그 경로에는 닿지 않는다.

    조회 뒤 반드시 commit한다 — psycopg_pool은 configure 함수가 끝난 뒤 연결이
    IDLE 상태가 아니면 그 연결을 그 자리에서 버린다(ProgrammingError). autocommit이
    아닌 연결에서 SELECT 하나만 해도 트랜잭션이 열린 채로 남아, 이 commit이 없으면
    매 연결마다 이 조회 자체가 원인이 되어 버려지고 pool이 새 연결을 못 얻어
    PoolTimeout으로 번진다(실측 — register_vector를 건너뛰도록 처음 고쳤을 때
    이 commit을 빠뜨려 같은 증상이 그대로 재현됐다).

    확장 유무는 이 함수가 불리는 연결 생성 시점에만 판정한다 — 앱이 계속 떠 있는
    채로 나중에 migration이 확장을 만들어도(예: 0003을 뒤늦게 적용) 이미 열려
    있던 기존 연결에는 반영되지 않는다. migration 적용 직후 gateway를 한 번
    재시작(rollout restart)해야 새로 열리는 연결들이 어댑터를 등록한다.
    """
    exists = conn.execute("SELECT 1 FROM pg_type WHERE typname = 'vector'").fetchone()
    conn.commit()
    if exists is None:
        global _pgvector_adapter_warning_logged
        if not _pgvector_adapter_warning_logged:
            # 연결마다 반복 출력되면 pool이 새 연결을 계속 여는 동안 로그가
            # 폭주한다 — 프로세스당 한 번만 알리면 충분하다(연결마다 알아야 할
            # 새 정보가 없다).
            logger.warning("pgvector adapter skipped: vector type not installed")
            _pgvector_adapter_warning_logged = True
        return
    register_vector(conn)


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
        configure=_configure_connection,
    )
    # DB가 꺼져 있어도 healthz를 제공해야 하므로, 기동 중 연결 성공을 기다리지 않는다.
    pool.open(wait=False)
    return pool
