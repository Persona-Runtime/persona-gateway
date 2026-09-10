from __future__ import annotations

import os
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
    PersonaLimitExceeded,
    PostgresPersonaStore,
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

        with PostgresContainer("postgres:16-alpine") as postgres:
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
