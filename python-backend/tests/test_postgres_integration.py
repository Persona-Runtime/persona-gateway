from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from persona_minimal_api.config import Settings
from persona_minimal_api.main import create_app
from persona_minimal_api.repository import (
    SUPPORTED_ALEMBIC_REVISIONS,
    DraftAlreadyExists,
    DraftNotFound,
    DraftSettings,
    DraftValidationError,
    IdempotencyConflict,
    PersonaLimitExceeded,
    PersonaNotFound,
    PostgresPersonaStore,
    RevisionConflict,
    create_pool,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def database_url() -> str:
    configured = os.getenv("TEST_DATABASE_URL")
    if configured:
        # 외부에서 준비한 격리 DB도 generator fixture가 실제 값으로 넘겨야 migration을 실행할 수 있다.
        yield configured
        return
    pytest.importorskip("testcontainers.postgres")
    try:
        from testcontainers.postgres import PostgresContainer

        # 0003부터 head migration이 pgvector 확장을 요구한다(material_chunks.embedding).
        # 이 fixture로 head까지 올리는 다른 모든 테스트도 이 이미지가 필요하다 — postgres:16
        # 기반이라 pgvector가 없는 것 말고는 동작이 같다.
        with PostgresContainer("pgvector/pgvector:pg16") as postgres:
            yield postgres.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    except Exception as exc:  # Docker is an optional local integration dependency.
        pytest.skip(f"isolated PostgreSQL is unavailable: {exc}")


def test_database_url_fixture_yields_configured_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://isolated-test-db")

    fixture_generator = database_url.__wrapped__()
    assert next(fixture_generator) == "postgresql://isolated-test-db"
    with pytest.raises(StopIteration):
        next(fixture_generator)


@pytest.fixture(scope="module")
def store(database_url: str) -> PostgresPersonaStore:
    root = Path(__file__).resolve().parents[1]
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        config = Config(str(root / "alembic.ini"))
        command.upgrade(config, "head")
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
    pool = create_pool(database_url, 2)
    yield PostgresPersonaStore(pool)
    pool.close()


def test_atomic_limit_and_idempotency_survive_recreation(store: PostgresPersonaStore) -> None:
    owner = f"owner-{uuid4()}"
    display_name = "합성 사용자"
    first_key = uuid4()
    first = store.create_persona(owner, display_name, "하나", first_key)
    store.create_persona(owner, display_name, "둘", uuid4())

    def create(name: str):
        return store.create_persona(owner, display_name, name, uuid4())

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(create, name) for name in ("셋", "넷")]
        outcomes = [future.exception() or future.result() for future in futures]
    assert sum(not isinstance(value, Exception) for value in outcomes) == 1
    assert sum(type(value).__name__ == "PersonaLimitExceeded" for value in outcomes) == 1
    assert store.create_persona(owner, display_name, "하나", first_key).id == first.id

    with store.pool.connection() as connection, connection.transaction():
        connection.execute(
            "UPDATE persona_minimal.personas SET deletion_id = %s WHERE id = %s",
            (uuid4(), first.id),
        )
    assert store.list_personas(owner, 10, None)[-1].status == "deleting"
    with pytest.raises(PersonaLimitExceeded):
        store.create_persona(owner, display_name, "삭제 중에도 한도", uuid4())

    replay_key = uuid4()
    replay_owner = f"owner-{uuid4()}"
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [
            workers.submit(store.create_persona, replay_owner, display_name, "동일", replay_key)
            for _ in range(2)
        ]
        personas = [future.result() for future in futures]
    assert personas[0].id == personas[1].id
    assert len(store.list_personas(replay_owner, 10, None)) == 1

    recreated = PostgresPersonaStore(store.pool)
    assert (
        recreated.create_persona(replay_owner, display_name, "동일", replay_key).id
        == personas[0].id
    )


def test_http_create_and_list_persist_through_new_app(store: PostgresPersonaStore) -> None:
    settings = Settings(
        DATABASE_URL="postgresql://unused",
        PERSONA_STATIC_BEARER_TOKEN="integration-token",
        PERSONA_STATIC_USER_ID=f"http-owner-{uuid4()}",
        PERSONA_STATIC_DISPLAY_NAME="통합 사용자",
        PERSONA_CURSOR_SIGNING_KEY="integration-cursor-key",
    )
    key = uuid4()
    headers = {"Authorization": "Bearer integration-token", "Idempotency-Key": str(key)}
    with TestClient(create_app(settings, store)) as first_app:
        assert first_app.get("/healthz").status_code == 200
        assert first_app.get("/readyz").json() == {"status": "ready"}
        assert first_app.get("/v1/me", headers=headers).status_code == 200
        created = first_app.post("/v1/personas", headers=headers, json={"name": "DB 캐릭터"})
        assert created.status_code == 201
        persona_id = created.json()["id"]

    with TestClient(create_app(settings, PostgresPersonaStore(store.pool))) as restarted_app:
        listed = restarted_app.get("/v1/personas", headers=headers)
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [persona_id]
        replay = restarted_app.post("/v1/personas", headers=headers, json={"name": "DB 캐릭터"})
        assert replay.status_code == 201
        assert replay.json()["id"] == persona_id


def test_readyz_times_out_when_schema_check_is_locked(store: PostgresPersonaStore) -> None:
    settings = Settings(
        DATABASE_URL="postgresql://unused",
        PERSONA_STATIC_BEARER_TOKEN="integration-token",
        PERSONA_STATIC_USER_ID=f"ready-owner-{uuid4()}",
        PERSONA_STATIC_DISPLAY_NAME="통합 사용자",
        PERSONA_CURSOR_SIGNING_KEY="integration-cursor-key",
    )
    with store.pool.connection() as locking_connection, locking_connection.transaction():
        locking_connection.execute(
            "LOCK TABLE persona_minimal.alembic_version IN ACCESS EXCLUSIVE MODE"
        )
        started = time.monotonic()
        with TestClient(create_app(settings, store)) as app:
            response = app.get("/readyz")
        elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert elapsed < 2.5


def _readiness_settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql://unused",
        PERSONA_STATIC_BEARER_TOKEN="integration-token",
        PERSONA_STATIC_USER_ID=f"revision-owner-{uuid4()}",
        PERSONA_STATIC_DISPLAY_NAME="통합 사용자",
        PERSONA_CURSOR_SIGNING_KEY="integration-cursor-key",
    )


def _set_revision(store: PostgresPersonaStore, revision: str) -> None:
    with store.pool.connection() as connection:
        connection.execute(
            "UPDATE persona_minimal.alembic_version SET version_num = %s", (revision,)
        )


def _readyz_with_revision(store: PostgresPersonaStore, revision: str) -> int:
    """alembic_version을 주어진 값으로 바꾼 상태에서 /readyz 상태 코드를 본다.

    변경을 커밋해야 한다. readiness는 pool의 다른 연결로 조회하므로 커밋하지 않은
    트랜잭션 안의 값은 보이지 않는다. 그래서 끝나면 원래 값으로 되돌린다.
    실제 migration을 돌리지 않고 readiness 판정만 바꿔 보기 위한 것이다.
    """
    with store.pool.connection() as connection:
        original = connection.execute(
            "SELECT version_num FROM persona_minimal.alembic_version"
        ).fetchone()[0]

    _set_revision(store, revision)
    try:
        with TestClient(create_app(_readiness_settings(), store)) as app:
            return app.get("/readyz").status_code
    finally:
        # 실패하더라도 뒤따르는 테스트가 깨지지 않게 반드시 되돌린다.
        _set_revision(store, original)


def test_readyz_requires_the_revision_this_release_supports(
    store: PostgresPersonaStore,
) -> None:
    """이 릴리스가 요구하는 revision에서만 Ready다.

    허용 목록을 넓히는 것은 **호환 릴리스의 역할**이지 기본값이 아니다. 새 migration을
    낼 때 구·신 revision을 함께 허용하는 릴리스를 먼저 배포해 적용 시점의 공백을 없앤다.
    그 규칙을 평소 릴리스로 가져오면, migration이 누락된 환경에서 Ready가 된 뒤
    실제 요청이 스키마 부재로 실패한다.

    revision 이름을 상수에서 읽지 않고 직접 적는다. 상수를 순회하면 허용 목록을
    바꿨을 때 검사 범위도 같이 바뀌어, 정작 막으려던 회귀를 놓친다.
    """
    # 0003은 0002와 함께 허용하는 호환 릴리스다(material_chunks 배포 공백을 없앤다).
    assert SUPPORTED_ALEMBIC_REVISIONS == ("0002_persona_draft", "0003_material_chunks")
    assert _readyz_with_revision(store, "0002_persona_draft") == 200
    assert _readyz_with_revision(store, "0003_material_chunks") == 200
    # 이 릴리스는 초안 테이블을 쓰므로 0001에서 Ready가 되면 안 된다. 그렇게 되면
    # migration이 누락된 환경에서 트래픽을 받은 뒤 요청이 테이블 부재로 실패한다.
    assert _readyz_with_revision(store, "0001_persona_minimal") == 503


def test_readyz_rejects_unknown_revision(store: PostgresPersonaStore) -> None:
    """목록에 없는 revision은 Ready로 보지 않는다.

    이것이 호환 릴리스를 먼저 배포해야 하는 이유의 재현이다 —
    목록에 없는 revision이 DB에 있으면 그 파드는 트래픽을 받지 못한다.
    """
    assert _readyz_with_revision(store, "9999_not_a_real_revision") == 503


def _settings(profile: str = "침착한 도서관 안내자다.") -> DraftSettings:
    return DraftSettings(name="합성 모루", profile=profile, speech_examples="")


def _persona_for(store: PostgresPersonaStore):
    owner = f"owner-{uuid4()}"
    persona = store.create_persona(owner, "합성 사용자", f"캐릭터-{uuid4().hex[:6]}", uuid4())
    return owner, persona


def test_draft_starts_without_a_job_and_persona_becomes_review_required(
    store: PostgresPersonaStore,
) -> None:
    """저장 전용 경로로 만든 초안은 job이 없다.

    job이 생기면 계약 2절에 따라 캐릭터가 `preparing`이 되고, 처리기가 없는 지금은
    거기서 영영 벗어나지 못한다. 초안만 있고 실행 중이 아니므로 `review_required`가 맞다.
    """
    owner, persona = _persona_for(store)
    assert store.get_persona(owner, persona.id).status == "needs_material"

    draft = store.create_draft(owner, persona.id, _settings(), uuid4())

    assert draft.revision == 1
    assert draft.status == "editing"
    assert draft.job_id is None
    assert draft.sources == ()
    # 처리기가 없으므로 적용할 수 없다. 여기서 True를 돌려주면 웹이 잘못 안내한다.
    assert draft.can_activate is False
    assert draft.requires_processing is True
    assert store.get_persona(owner, persona.id).status == "review_required"


def test_second_draft_is_rejected(store: PostgresPersonaStore) -> None:
    owner, persona = _persona_for(store)
    store.create_draft(owner, persona.id, _settings(), uuid4())

    with pytest.raises(DraftAlreadyExists):
        store.create_draft(owner, persona.id, _settings("다른 소개다."), uuid4())


def test_same_key_replays_the_draft_instead_of_conflicting(
    store: PostgresPersonaStore,
) -> None:
    """응답이 유실된 생성 요청의 재전송이다.

    초안이 이미 있다는 이유로 409를 내면 요청자는 실패한 줄 알고 되돌리려 한다.
    """
    owner, persona = _persona_for(store)
    key = uuid4()
    first = store.create_draft(owner, persona.id, _settings(), key)
    replayed = store.create_draft(owner, persona.id, _settings(), key)

    assert replayed.version_id == first.version_id
    assert replayed.revision == first.revision

    with pytest.raises(IdempotencyConflict):
        store.create_draft(owner, persona.id, _settings("다른 소개다."), key)


def test_patch_bumps_revision_and_stores_sources(store: PostgresPersonaStore) -> None:
    owner, persona = _persona_for(store)
    draft = store.create_draft(owner, persona.id, _settings(), uuid4())

    patched = store.patch_draft(
        owner,
        persona.id,
        draft.revision,
        {"profile": "침착하며 모르는 것은 모른다고 말한다."},
        [{"kind": "events", "filename": "events.md", "content": "개관 첫날 지도책을 찾아냈다."}],
        [],
    )

    assert patched.revision == draft.revision + 1
    assert patched.settings.profile == "침착하며 모르는 것은 모른다고 말한다."
    assert len(patched.sources) == 1
    source = patched.sources[0]
    assert source.kind == "events"
    assert source.byte_size == len(source.content.encode("utf-8"))
    assert re.fullmatch(r"[0-9a-f]{64}", source.sha256)

    removed = store.patch_draft(owner, persona.id, patched.revision, None, [], [source.id])
    assert removed.sources == ()


def test_stale_expected_revision_is_a_conflict(store: PostgresPersonaStore) -> None:
    """다른 사람의 수정을 덮지 않는다.

    CAS가 없으면 낡은 화면에서 보낸 수정이 그 사이의 변경을 조용히 지운다.
    """
    owner, persona = _persona_for(store)
    draft = store.create_draft(owner, persona.id, _settings(), uuid4())
    store.patch_draft(owner, persona.id, draft.revision, {"name": "새 이름"}, [], [])

    with pytest.raises(RevisionConflict):
        store.patch_draft(owner, persona.id, draft.revision, {"name": "더 새 이름"}, [], [])


def test_patch_rejects_contract_violations(store: PostgresPersonaStore) -> None:
    owner, persona = _persona_for(store)
    draft = store.create_draft(owner, persona.id, _settings(), uuid4())
    revision = draft.revision

    def patch(**kwargs):
        return store.patch_draft(
            owner,
            persona.id,
            kwargs.pop("revision", revision),
            kwargs.pop("settings", None),
            kwargs.pop("upsert", []),
            kwargs.pop("remove", []),
        )

    # 설정과 같은 종류의 자료를 한 요청에서 함께 고치면 두 진실이 생긴다(계약 5절).
    with pytest.raises(DraftValidationError) as conflicting:
        patch(settings={"profile": "새 소개"}, upsert=[{"kind": "profile", "content": "다른 소개"}])
    assert conflicting.value.code == "conflicting_fields"

    # 비공백 profile은 최종 초안에서도 필수다.
    with pytest.raises(DraftValidationError) as blank:
        patch(settings={"profile": "   "})
    assert blank.value.code == "invalid_settings"

    # 현재 초안의 것이 아닌 id는 거부한다.
    with pytest.raises(DraftValidationError) as unknown:
        patch(remove=[uuid4()])
    assert unknown.value.code == "unknown_source"

    # 파일 출처 자료 1 MiB 상한(계약 5절).
    with pytest.raises(DraftValidationError) as too_large:
        patch(upsert=[{"kind": "events", "content": "가" * 400_000}])
    assert too_large.value.status == 413


def test_discard_removes_draft_and_sources(store: PostgresPersonaStore) -> None:
    owner, persona = _persona_for(store)
    draft = store.create_draft(owner, persona.id, _settings(), uuid4())
    store.patch_draft(
        owner, persona.id, draft.revision, None, [{"kind": "events", "content": "자료"}], []
    )
    key = uuid4()

    store.discard_draft(owner, persona.id, key)
    # 응답이 유실된 폐기 요청의 재전송이다. 404를 내면 실패한 줄 알고 되돌리려 한다.
    store.discard_draft(owner, persona.id, key)

    with pytest.raises(DraftNotFound):
        store.get_draft(owner, persona.id)
    # 캐릭터는 남는다.
    assert store.get_persona(owner, persona.id).status == "needs_material"

    with store.pool.connection() as connection:
        left = connection.execute(
            "SELECT count(*) FROM persona_minimal.material_sources WHERE persona_id = %s",
            (persona.id,),
        ).fetchone()[0]
    assert left == 0


def test_other_owner_cannot_reach_the_draft(store: PostgresPersonaStore) -> None:
    owner, persona = _persona_for(store)
    store.create_draft(owner, persona.id, _settings(), uuid4())

    # 타인 소유와 부재를 같은 오류로 올린다. 구분하면 존재 여부가 새어 나간다.
    with pytest.raises(PersonaNotFound):
        store.get_draft(f"owner-{uuid4()}", persona.id)


@pytest.fixture(scope="module")
def round_trip_database_url() -> str:
    # 0003 upgrade/downgrade 왕복 전용 컨테이너. 위 `store` fixture(모듈 전체가 공유)
    # 데이터베이스에 downgrade를 걸면 그 뒤에 도는 다른 테스트들이 head 스키마를
    # 전제로 실패한다 — 그래서 별도 컨테이너로 완전히 분리한다.
    configured = os.getenv("TEST_ROUND_TRIP_DATABASE_URL")
    if configured:
        yield configured
        return
    pytest.importorskip("testcontainers.postgres")
    try:
        from testcontainers.postgres import PostgresContainer

        with PostgresContainer("pgvector/pgvector:pg16") as postgres:
            yield postgres.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    except Exception as exc:  # Docker is an optional local integration dependency.
        pytest.skip(f"isolated PostgreSQL is unavailable: {exc}")


def _column_exists(connection, schema: str, table: str, column: str) -> bool:
    count = connection.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
        (schema, table, column),
    ).fetchone()[0]
    return bool(count)


def _table_exists(connection, schema: str, table: str) -> bool:
    count = connection.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        (schema, table),
    ).fetchone()[0]
    return bool(count)


def test_migration_0003_upgrade_and_downgrade_round_trip(round_trip_database_url: str) -> None:
    root = Path(__file__).resolve().parents[1]
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = round_trip_database_url
    try:
        config = Config(str(root / "alembic.ini"))
        command.upgrade(config, "head")

        pool = create_pool(round_trip_database_url, 2)
        try:
            with pool.connection() as connection:
                assert _table_exists(connection, "persona_minimal", "material_chunks")
                assert _column_exists(
                    connection, "persona_minimal", "material_versions", "error_code"
                )
                assert _column_exists(
                    connection, "persona_minimal", "material_versions", "indexed_revision"
                )
                assert _column_exists(
                    connection, "persona_minimal", "material_versions", "indexed_at"
                )
                unique_constraints = connection.execute(
                    """
                    SELECT count(*) FROM information_schema.table_constraints
                    WHERE table_schema = 'persona_minimal'
                      AND table_name = 'material_chunks'
                      AND constraint_type = 'UNIQUE'
                    """
                ).fetchone()[0]
                assert unique_constraints == 1
        finally:
            pool.close()

        command.downgrade(config, "-1")

        pool = create_pool(round_trip_database_url, 2)
        try:
            with pool.connection() as connection:
                assert not _table_exists(connection, "persona_minimal", "material_chunks")
                assert not _column_exists(
                    connection, "persona_minimal", "material_versions", "error_code"
                )
                assert not _column_exists(
                    connection, "persona_minimal", "material_versions", "indexed_revision"
                )
                assert not _column_exists(
                    connection, "persona_minimal", "material_versions", "indexed_at"
                )
        finally:
            pool.close()

        # 반복 가능성: 같은 head로 다시 올려도 문제없어야 한다.
        command.upgrade(config, "head")
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
