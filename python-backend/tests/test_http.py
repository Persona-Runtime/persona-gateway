from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import (
    AdminShutdown,
    CannotConnectNow,
    ConnectionFailure,
    InsufficientPrivilege,
    InvalidPassword,
    TooManyConnections,
    UndefinedTable,
)
from psycopg_pool import PoolTimeout

from persona_minimal_api.config import Settings
from persona_minimal_api.cursor import PersonaCursor
from persona_minimal_api.main import create_app
from persona_minimal_api.repository import (
    DuplicatePersonaName,
    IdempotencyConflict,
    Persona,
    PersonaLimitExceeded,
)


class MemoryStore:
    def __init__(self) -> None:
        self.personas: list[Persona] = []
        self.requests: dict[UUID, tuple[str, Persona]] = {}

    def list_personas(self, owner: str, limit: int, cursor: PersonaCursor | None) -> list[Persona]:
        rows = [persona for persona in self.personas if persona.deleted_at is None]
        rows.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        if cursor is not None:
            rows = [
                item
                for item in rows
                if (item.created_at, item.id) < (cursor.created_at, cursor.persona_id)
            ]
        return rows[:limit]

    def create_persona(self, owner: str, display_name: str, name: str, key: UUID) -> Persona:
        existing = self.requests.get(key)
        if existing is not None:
            if existing[0] != name:
                raise IdempotencyConflict
            return existing[1]
        if any(item.name == name and item.deleted_at is None for item in self.personas):
            raise DuplicatePersonaName
        if len([item for item in self.personas if item.deleted_at is None]) >= 3:
            raise PersonaLimitExceeded
        persona = Persona(uuid4(), name, datetime.now(UTC), None, None)
        self.personas.append(persona)
        self.requests[key] = (name, persona)
        return persona

    def is_ready(self) -> bool:
        return True


class FailingStore(MemoryStore):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def list_personas(self, owner: str, limit: int, cursor: PersonaCursor | None) -> list[Persona]:
        raise self.error


class UnexpectedFailureStore(MemoryStore):
    def list_personas(self, owner: str, limit: int, cursor: PersonaCursor | None) -> list[Persona]:
        raise RuntimeError("synthetic unexpected failure")


def settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql://unused",
        PERSONA_STATIC_BEARER_TOKEN="synthetic-token",
        PERSONA_STATIC_USER_ID="synthetic-owner",
        PERSONA_STATIC_DISPLAY_NAME="합성 사용자",
        PERSONA_CURSOR_SIGNING_KEY="synthetic-cursor-key",
    )


def client(store: MemoryStore | None = None) -> TestClient:
    return TestClient(create_app(settings(), store or MemoryStore()))


def headers(key: UUID | None = None) -> dict[str, str]:
    result = {"Authorization": "Bearer synthetic-token"}
    if key is not None:
        result["Idempotency-Key"] = str(key)
    return result


def test_me_requires_static_bearer_token() -> None:
    api = client()
    missing = api.get("/v1/me")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
    assert missing.headers["cache-control"] == "no-store"

    response = api.get("/v1/me", headers=headers())
    assert response.status_code == 200
    assert response.json() == {"id": "synthetic-owner", "display_name": "합성 사용자"}


def test_healthz_ignores_database_and_readyz_is_safe() -> None:
    api = client()
    health = api.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    ready = api.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}

    class NotReadyStore(MemoryStore):
        def is_ready(self) -> bool:
            return False

    unavailable = client(NotReadyStore()).get("/readyz")
    assert unavailable.status_code == 503
    assert unavailable.json() == {"status": "not_ready"}
    assert unavailable.headers["cache-control"] == "no-store"


def test_non_ascii_bearer_token_is_unauthorized() -> None:
    response = client().get("/v1/me", headers={"Authorization": b"Bearer caf\xe9"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "error",
    [
        ConnectionFailure("synthetic connection failure"),
        PoolTimeout("synthetic pool timeout"),
        TooManyConnections("synthetic connection limit"),
        AdminShutdown("synthetic database shutdown"),
        CannotConnectNow("synthetic database startup"),
    ],
    ids=["connection_failure", "pool_timeout", "connection_limit", "shutdown", "startup"],
)
def test_transient_database_failure_uses_safe_common_error_response(error: Exception) -> None:
    response = client(FailingStore(error)).get("/v1/personas", headers=headers())

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "dependency_unavailable",
        "message": "잠시 후 다시 시도해주세요.",
        "request_id": response.headers["x-request-id"],
    }
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "error",
    [
        UndefinedTable("synthetic missing table"),
        InsufficientPrivilege("synthetic missing privilege"),
        InvalidPassword("synthetic invalid password"),
    ],
    ids=["missing_table", "missing_privilege", "invalid_password"],
)
def test_non_transient_database_failure_is_safe_internal_error(error: Exception) -> None:
    api = TestClient(create_app(settings(), FailingStore(error)), raise_server_exceptions=False)
    response = api.get("/v1/personas", headers=headers())

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert str(error) not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"] == response.json()["error"]["request_id"]


def test_unexpected_failure_does_not_expose_exception_details() -> None:
    api = TestClient(
        create_app(settings(), UnexpectedFailureStore()), raise_server_exceptions=False
    )
    response = api.get("/v1/personas", headers=headers())

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "synthetic unexpected failure" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"] == response.json()["error"]["request_id"]


def test_list_is_empty_then_creates_snake_case_persona() -> None:
    api = client()
    assert api.get("/v1/personas", headers=headers()).json() == {"items": [], "next_cursor": None}

    response = api.post("/v1/personas", headers=headers(uuid4()), json={"name": "  합성 모루  "})
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "합성 모루"
    assert body["status"] == "needs_material"
    assert body["active_version_id"] is None
    assert body["draft"] is None
    assert body["deletion_id"] is None
    assert "created_at" in body


def test_create_validates_name_and_idempotency_key() -> None:
    api = client()
    missing_key = api.post("/v1/personas", headers=headers(), json={"name": "모루"})
    assert missing_key.status_code == 400
    assert missing_key.json()["error"]["code"] == "invalid_idempotency_key"

    blank = api.post("/v1/personas", headers=headers(uuid4()), json={"name": " \t "})
    assert blank.status_code == 422
    assert blank.json()["error"]["fields"] == [{"field": "name", "code": "blank"}]

    nul = api.post("/v1/personas", headers=headers(uuid4()), json={"name": "모\x00루"})
    assert nul.status_code == 422
    assert nul.json()["error"]["fields"] == [{"field": "name", "code": "contains_nul"}]

    invalid_body = api.post(
        "/v1/personas", headers=headers(uuid4()), json={"name": "모루", "owner_subject": "other"}
    )
    assert invalid_body.status_code == 422
    assert invalid_body.json()["error"]["code"] == "invalid_request"


def test_idempotency_replays_or_rejects_changed_input() -> None:
    api = client()
    key = uuid4()
    first = api.post("/v1/personas", headers=headers(key), json={"name": "모루"})
    replay = api.post("/v1/personas", headers=headers(key), json={"name": "모루"})
    assert first.status_code == replay.status_code == 201
    assert first.json()["id"] == replay.json()["id"]

    conflict = api.post("/v1/personas", headers=headers(key), json={"name": "다른 이름"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_pagination_rejects_invalid_cursor_and_limit() -> None:
    store = MemoryStore()
    now = datetime.now(UTC)
    store.personas = [
        Persona(uuid4(), "첫", now, None, None),
        Persona(uuid4(), "둘", now - timedelta(seconds=1), None, None),
    ]
    api = client(store)
    page = api.get("/v1/personas?limit=1", headers=headers())
    assert page.status_code == 200
    assert page.json()["next_cursor"] is not None
    next_page = api.get(
        "/v1/personas", headers=headers(), params={"cursor": page.json()["next_cursor"]}
    )
    assert next_page.status_code == 200
    assert len(next_page.json()["items"]) == 1
    assert api.get("/v1/personas?limit=101", headers=headers()).status_code == 400
    assert api.get("/v1/personas?cursor=not-a-cursor", headers=headers()).status_code == 400
