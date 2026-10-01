"""회원가입·로그인·로그아웃·세션 인증 규칙(A-1).

판정 규칙은 auth/service.py에 있고, 여기서는 그 규칙을 메모리 저장소와 HTTP 앱으로 검사한다.
DB 고유의 보장(UNIQUE 경합, FOR UPDATE 직렬화, 사용자별 캐릭터 격리)은
test_postgres_integration.py가 실제 Postgres로 검사한다.

모든 아이디·비밀번호·토큰은 합성 값이다.
"""

from __future__ import annotations

import copy
import logging
import statistics
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from persona_minimal_api.auth.service import (
    LAST_SEEN_MIN_INTERVAL_SECONDS,
    AuthPolicy,
    AuthService,
    hash_token,
)
from persona_minimal_api.auth.store import (
    AuthSchemaNotReady,
    Credential,
    SessionRecord,
    UsernameTaken,
)
from persona_minimal_api.config import Settings
from persona_minimal_api.main import create_app

STATIC_TOKEN = "synthetic-static-token"
PASSWORD = "synthetic-pass-1"
WRONG_PASSWORD = "synthetic-wrong-1"

BASE_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql://unused",
    "PERSONA_EMBEDDING_URL": "http://embedding.invalid",
    "PERSONA_STATIC_BEARER_TOKEN": STATIC_TOKEN,
    "PERSONA_STATIC_USER_ID": "synthetic-static-user",
    "PERSONA_STATIC_DISPLAY_NAME": "합성 정적 사용자",
    "PERSONA_CURSOR_SIGNING_KEY": "synthetic-cursor-key",
}


class MemoryAuthStore:
    """PostgresAuthStore와 같은 계약을 메모리로 흉내 낸다. `now`를 바꿔 시간을 주입한다."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        self.display_names: dict[str, str] = {}
        self.credentials: dict[str, dict] = {}
        self.sessions: dict[bytes, dict] = {}
        self.touch_count = 0

    def create_account(
        self,
        subject: str,
        username: str,
        password_hash: str,
        token_hash: bytes,
        ttl_seconds: int,
    ) -> datetime:
        if username in self.credentials:
            raise UsernameTaken
        self.display_names[subject] = username
        self.credentials[username] = {
            "subject": subject,
            "password_hash": password_hash,
            "failed_attempts": 0,
            "locked_until": None,
        }
        return self._insert_session(token_hash, subject, ttl_seconds)

    @contextmanager
    def login_attempt(self, username: str) -> Iterator[_MemoryLoginAttempt]:
        # 실제 저장소처럼 트랜잭션으로 흉내 낸다: 블록이 예외로 끝나면 그 안의 기록을 버린다.
        # 이 롤백을 흉내 내지 않으면 "실패 기록 뒤 블록 안에서 예외"라는 버그를 놓친다.
        snapshot = copy.deepcopy((self.credentials, self.sessions))
        try:
            yield _MemoryLoginAttempt(self, username)
        except BaseException:
            self.credentials, self.sessions = snapshot
            raise

    def find_session(self, token_hash: bytes) -> SessionRecord | None:
        row = self.sessions.get(token_hash)
        if row is None:
            return None
        return SessionRecord(
            subject=row["subject"],
            display_name=self.display_names[row["subject"]],
            expires_at=row["expires_at"],
            last_seen_at=row["last_seen_at"],
            revoked_at=row["revoked_at"],
            now=self.now,
        )

    def touch_session(self, token_hash: bytes, min_interval_seconds: int) -> None:
        self.touch_count += 1
        self.sessions[token_hash]["last_seen_at"] = self.now

    def revoke_session(self, token_hash: bytes) -> None:
        self.sessions[token_hash]["revoked_at"] = self.now

    def _insert_session(self, token_hash: bytes, subject: str, ttl_seconds: int) -> datetime:
        expires_at = self.now + timedelta(seconds=ttl_seconds)
        self.sessions[token_hash] = {
            "subject": subject,
            "expires_at": expires_at,
            "last_seen_at": None,
            "revoked_at": None,
        }
        return expires_at


class _MemoryLoginAttempt:
    def __init__(self, store: MemoryAuthStore, username: str) -> None:
        self._store = store
        self._row = store.credentials.get(username)
        self.now = store.now
        self.credential = (
            None
            if self._row is None
            else Credential(
                subject=self._row["subject"],
                display_name=store.display_names[self._row["subject"]],
                password_hash=self._row["password_hash"],
                failed_attempts=self._row["failed_attempts"],
                locked_until=self._row["locked_until"],
            )
        )

    def record_failure(self, failed_attempts: int, locked_until: datetime | None) -> None:
        self._row["failed_attempts"] = failed_attempts
        self._row["locked_until"] = locked_until

    def record_success(self, token_hash: bytes, ttl_seconds: int) -> datetime:
        self._row["failed_attempts"] = 0
        self._row["locked_until"] = None
        return self._store._insert_session(token_hash, self._row["subject"], ttl_seconds)


class EmptyPersonaStore:
    """인증 테스트에 필요한 만큼만 있는 persona 저장소. 캐릭터는 항상 비어 있다."""

    def list_personas(self, owner: str, limit: int, cursor: object) -> list:
        return []

    def is_ready(self) -> bool:
        return True


@pytest.fixture
def auth_store() -> MemoryAuthStore:
    return MemoryAuthStore()


def make_client(auth_store: MemoryAuthStore, **env: str) -> TestClient:
    settings = Settings(**BASE_ENV, **env)
    return TestClient(create_app(settings, EmptyPersonaStore(), auth_store=auth_store))


@pytest.fixture
def client(auth_store: MemoryAuthStore) -> Iterator[TestClient]:
    with make_client(auth_store) as test_client:
        yield test_client


def signup(client: TestClient, username: str = "synthetic_user", password: str = PASSWORD):
    return client.post("/v1/auth/signup", json={"username": username, "password": password})


def login(client: TestClient, username: str = "synthetic_user", password: str = PASSWORD):
    return client.post("/v1/auth/login", json={"username": username, "password": password})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- 가입 ---


def test_signup_issues_session_for_new_local_user(client: TestClient) -> None:
    response = signup(client)

    assert response.status_code == 201
    body = response.json()
    assert body["user"]["id"].startswith("local:")
    assert body["user"]["display_name"] == "synthetic_user"
    assert body["token"]
    assert datetime.fromisoformat(body["expires_at"]).tzinfo is not None


def test_signup_session_expires_after_configured_ttl(
    client: TestClient, auth_store: MemoryAuthStore
) -> None:
    body = signup(client).json()

    assert datetime.fromisoformat(body["expires_at"]) == auth_store.now + timedelta(days=7)


def test_signup_lowercases_username_before_storing(client: TestClient) -> None:
    response = signup(client, username="Synthetic_Alice")

    assert response.status_code == 201
    assert response.json()["user"]["display_name"] == "synthetic_alice"
    assert login(client, username="synthetic_alice").status_code == 200


@pytest.mark.parametrize(
    ("username", "password", "field"),
    [
        ("ab", PASSWORD, "username"),  # 3자 미만
        ("a" * 33, PASSWORD, "username"),  # 32자 초과
        ("bad-name", PASSWORD, "username"),  # 허용하지 않는 문자
        ("synthetic_user", "short7!", "password"),  # 8 코드포인트 미만
        ("synthetic_user", "x" * 129, "password"),  # 128 코드포인트 초과
        ("synthetic_user", " " * 10, "password"),  # 공백만
    ],
)
def test_signup_rejects_rule_violations_with_422(
    client: TestClient, username: str, password: str, field: str
) -> None:
    response = signup(client, username=username, password=password)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert [item["field"] for item in error["fields"]] == [field]


def test_password_length_counts_code_points_not_bytes(client: TestClient) -> None:
    # 한글 8자는 UTF-8로 24바이트지만 8 코드포인트라 허용된다.
    assert signup(client, password="합성비밀번호예시").status_code == 201


def test_signup_rejects_duplicate_username_ignoring_case(client: TestClient) -> None:
    assert signup(client, username="synthetic_bob").status_code == 201

    response = signup(client, username="SYNTHETIC_BOB")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "username_taken"


def test_signup_disabled_returns_403_before_checking_input(auth_store: MemoryAuthStore) -> None:
    with make_client(auth_store, PERSONA_SIGNUP_ENABLED="false") as disabled_client:
        # 규칙 위반 입력이어도 422가 아니라 403이다 — 가입이 꺼졌다는 사실이 먼저다.
        response = signup(disabled_client, username="x")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "signup_disabled"
    assert auth_store.credentials == {}


# --- 로그인 ---


def test_login_succeeds_with_correct_password(client: TestClient) -> None:
    signup(client)

    response = login(client)

    assert response.status_code == 200
    assert response.json()["user"]["display_name"] == "synthetic_user"
    assert response.json()["token"]


@pytest.mark.parametrize(
    ("username", "password"),
    [("synthetic_user", WRONG_PASSWORD), ("synthetic_nobody", PASSWORD)],
    ids=["wrong-password", "unknown-username"],
)
def test_login_failures_share_one_401_code(
    client: TestClient, username: str, password: str
) -> None:
    signup(client)

    response = login(client, username=username, password=password)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_login_locks_after_threshold_failures_even_for_correct_password(
    client: TestClient,
) -> None:
    signup(client)
    for _ in range(5):
        assert login(client, password=WRONG_PASSWORD).status_code == 401

    response = login(client)

    assert response.status_code == 423
    assert response.json()["error"]["code"] == "account_locked"
    assert response.headers["Retry-After"] == "900"


def test_login_succeeds_after_lock_expires(client: TestClient, auth_store: MemoryAuthStore) -> None:
    signup(client)
    for _ in range(5):
        login(client, password=WRONG_PASSWORD)

    auth_store.now += timedelta(seconds=901)

    assert login(client).status_code == 200


def test_failure_count_restarts_after_lock_expires(
    client: TestClient, auth_store: MemoryAuthStore
) -> None:
    """잠금이 풀린 뒤 한 번 틀렸다고 바로 다시 잠기지 않는다."""
    signup(client)
    for _ in range(5):
        login(client, password=WRONG_PASSWORD)
    auth_store.now += timedelta(seconds=901)

    assert login(client, password=WRONG_PASSWORD).status_code == 401
    assert login(client).status_code == 200


def test_success_resets_failure_count(client: TestClient) -> None:
    signup(client)
    for _ in range(4):
        login(client, password=WRONG_PASSWORD)
    assert login(client).status_code == 200

    for _ in range(4):
        login(client, password=WRONG_PASSWORD)

    assert login(client).status_code == 200


def test_unknown_username_takes_about_as_long_as_wrong_password(client: TestClient) -> None:
    """없는 아이디도 argon2 검증 1회를 쓴다. 응답 시간으로 계정 존재를 가르지 못해야 한다.

    정확한 시간 비교가 아니라 "없는 아이디가 확연히 빠르지 않다"는 상한·하한 확인이다.
    """
    signup(client)
    login(client, username="synthetic_warmup")  # 더미 해시를 처음 만드는 비용은 빼고 잰다.

    def median_seconds(username: str, password: str) -> float:
        samples = []
        for _ in range(3):
            started = time.perf_counter()
            login(client, username=username, password=password)
            samples.append(time.perf_counter() - started)
        return statistics.median(samples)

    wrong_password = median_seconds("synthetic_user", WRONG_PASSWORD)
    unknown_user = median_seconds("synthetic_nobody", PASSWORD)
    # 잠금에 닿지 않도록 위에서 틀린 횟수는 3회로 묶었다.
    assert 0.5 * wrong_password <= unknown_user <= 2.0 * wrong_password


# --- 세션 토큰 ---


def test_session_token_authenticates_me(client: TestClient) -> None:
    issued = signup(client).json()

    response = client.get("/v1/me", headers=bearer(issued["token"]))

    assert response.status_code == 200
    assert response.json() == issued["user"]


def test_two_users_see_their_own_identity(client: TestClient) -> None:
    first = signup(client, username="synthetic_one").json()
    second = signup(client, username="synthetic_two").json()

    first_me = client.get("/v1/me", headers=bearer(first["token"])).json()
    second_me = client.get("/v1/me", headers=bearer(second["token"])).json()

    assert first_me["id"] != second_me["id"]
    assert first_me["display_name"] == "synthetic_one"
    assert second_me["display_name"] == "synthetic_two"


def test_logout_revokes_the_session(client: TestClient) -> None:
    token = signup(client).json()["token"]

    assert client.post("/v1/auth/logout", headers=bearer(token)).status_code == 204

    assert client.get("/v1/me", headers=bearer(token)).status_code == 401
    # 이미 취소된 토큰으로 다시 로그아웃하면 401이다.
    assert client.post("/v1/auth/logout", headers=bearer(token)).status_code == 401


def test_logout_without_session_token_is_401(client: TestClient) -> None:
    assert client.post("/v1/auth/logout").status_code == 401
    # 정적 토큰은 세션이 아니라 로그아웃 대상이 아니다.
    assert client.post("/v1/auth/logout", headers=bearer(STATIC_TOKEN)).status_code == 401


def test_expired_session_is_rejected(client: TestClient, auth_store: MemoryAuthStore) -> None:
    token = signup(client).json()["token"]

    auth_store.now += timedelta(days=7)  # expires_at == now 이면 이미 만료다.

    assert client.get("/v1/me", headers=bearer(token)).status_code == 401
    assert client.post("/v1/auth/logout", headers=bearer(token)).status_code == 401


def test_unknown_token_is_rejected(client: TestClient) -> None:
    response = client.get("/v1/me", headers=bearer("synthetic-not-a-session"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_session_token_is_stored_only_as_sha256(
    client: TestClient, auth_store: MemoryAuthStore
) -> None:
    token = signup(client).json()["token"]

    assert list(auth_store.sessions) == [hash_token(token)]


def test_last_seen_is_updated_at_most_once_per_interval(auth_store: MemoryAuthStore) -> None:
    service = AuthService(
        auth_store,
        AuthPolicy(
            signup_enabled=True,
            session_ttl_seconds=3600,
            login_lock_threshold=5,
            login_lock_seconds=900,
        ),
    )
    token = service.signup("synthetic_user", PASSWORD).token

    service.authenticate(token)  # 첫 사용: last_seen_at이 비어 있어 갱신한다.
    auth_store.now += timedelta(seconds=LAST_SEEN_MIN_INTERVAL_SECONDS - 1)
    service.authenticate(token)  # 간격 안: 갱신하지 않는다.
    assert auth_store.touch_count == 1

    auth_store.now += timedelta(seconds=1)
    service.authenticate(token)  # 간격에 닿음: 다시 갱신한다.
    assert auth_store.touch_count == 2


# --- 정적 토큰 전환 ---


def test_static_token_still_works_when_enabled(client: TestClient) -> None:
    response = client.get("/v1/me", headers=bearer(STATIC_TOKEN))

    assert response.status_code == 200
    assert response.json()["id"] == "synthetic-static-user"


def test_static_token_is_rejected_when_disabled(auth_store: MemoryAuthStore) -> None:
    with make_client(auth_store, PERSONA_STATIC_TOKEN_ENABLED="false") as session_only:
        token = signup(session_only).json()["token"]

        assert session_only.get("/v1/me", headers=bearer(STATIC_TOKEN)).status_code == 401
        # 세션 토큰은 정적 토큰 설정과 무관하게 동작한다.
        assert session_only.get("/v1/me", headers=bearer(token)).status_code == 200


def test_session_token_reaches_protected_routes(client: TestClient) -> None:
    token = signup(client).json()["token"]

    response = client.get("/v1/personas", headers=bearer(token))

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_auth_routes_are_unavailable_without_auth_store() -> None:
    """DB 없는 fake persona 저장소로 만든 앱에는 세션 저장소가 없다 — 조용히 성공하지 않는다."""
    with TestClient(create_app(Settings(**BASE_ENV), EmptyPersonaStore())) as no_auth:
        response = signup(no_auth)

    assert response.status_code == 503


# --- 비밀값 노출 ---


def test_logs_never_contain_password_token_or_hash(
    client: TestClient, auth_store: MemoryAuthStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    token = signup(client).json()["token"]
    login(client, password=WRONG_PASSWORD)
    second_token = login(client).json()["token"]
    client.get("/v1/me", headers=bearer(token))
    client.post("/v1/auth/logout", headers=bearer(second_token))

    stored_hash = auth_store.credentials["synthetic_user"]["password_hash"]
    secrets = [
        PASSWORD,
        WRONG_PASSWORD,
        token,
        second_token,
        stored_hash,
        hash_token(token).hex(),
    ]
    assert caplog.records, "auth 결과 로그가 실제로 남아야 이 검사가 의미 있다"
    for secret in secrets:
        assert secret not in caplog.text


# --- A-1 bridge: DB가 아직 0005(auth 테이블 없음) ---


class SchemaMissingAuthStore:
    """PostgresAuthStore가 0005 DB에서 보이는 모습: 모든 auth 테이블 접근이 AuthSchemaNotReady."""

    def create_account(self, *args: object, **kwargs: object) -> datetime:
        raise AuthSchemaNotReady

    @contextmanager
    def login_attempt(self, username: str) -> Iterator[None]:
        raise AuthSchemaNotReady
        yield  # contextmanager가 generator를 요구한다. 위에서 항상 끝난다.

    def find_session(self, token_hash: bytes) -> SessionRecord | None:
        raise AuthSchemaNotReady


@pytest.fixture
def bridge_client() -> Iterator[TestClient]:
    settings = Settings(**BASE_ENV)
    app = create_app(settings, EmptyPersonaStore(), auth_store=SchemaMissingAuthStore())
    with TestClient(app) as test_client:
        yield test_client


def test_auth_routes_return_503_until_auth_tables_exist(bridge_client: TestClient) -> None:
    responses = [
        signup(bridge_client),
        login(bridge_client),
        bridge_client.post("/v1/auth/logout", headers=bearer("synthetic-session-token")),
    ]

    assert [response.status_code for response in responses] == [503, 503, 503]
    assert {response.json()["error"]["code"] for response in responses} == {
        "dependency_unavailable"
    }


def test_other_routes_keep_working_without_auth_tables(bridge_client: TestClient) -> None:
    """auth 테이블이 없어도 정적 토큰 요청은 200, 그 밖의 토큰은 503이 아니라 401이다."""
    assert bridge_client.get("/v1/me", headers=bearer(STATIC_TOKEN)).status_code == 200
    assert bridge_client.get("/v1/personas", headers=bearer(STATIC_TOKEN)).status_code == 200

    unknown = bridge_client.get("/v1/me", headers=bearer("synthetic-session-token"))

    assert unknown.status_code == 401
    assert unknown.json()["error"]["code"] == "unauthorized"
