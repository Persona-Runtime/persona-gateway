"""가입·로그인·로그아웃·세션 판정 규칙.

HTTP와 DB를 모른다. 입력은 이미 형식 검증을 통과한 값이고, 결과는 값 또는 아래 예외다.
main.py가 예외를 ApiError(상태 코드·오류 코드)로 바꾼다.

토큰 원문은 발급 응답에만 존재하고, 저장·조회는 sha256 해시로만 한다.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from .passwords import PasswordHashing
from .store import AuthSchemaNotReady, AuthStore, LoginAttempt, SessionRecord

# 세션 last_seen_at은 이 간격보다 자주 갱신하지 않는다. 인증마다 UPDATE하면 읽기 요청이 전부
# 쓰기가 되기 때문이다. 값은 "대략 언제까지 쓰였는가"를 보는 용도라 분 단위면 충분하다.
LAST_SEEN_MIN_INTERVAL_SECONDS = 60

# token_urlsafe(32)는 32바이트(256비트) 난수를 약 43자로 인코딩한다.
SESSION_TOKEN_BYTES = 32

LOCAL_SUBJECT_PREFIX = "local:"

# 계약의 username 규칙. 소문자로 바꾼 뒤 검사하므로 "Alice"는 "alice"로 가입된다.
USERNAME_PATTERN = re.compile(r"^[a-z0-9_]{3,32}$")
# 길이는 바이트가 아니라 코드포인트(파이썬 len) 기준이다. 상한은 해시 입력 크기를 묶어 둔다.
PASSWORD_MIN_CODE_POINTS = 8
PASSWORD_MAX_CODE_POINTS = 128


class SignupDisabled(Exception):
    pass


class CredentialsRuleViolation(Exception):
    """가입 입력이 규칙에 맞지 않는다. fields는 계약 Error.fields 형식이다."""

    def __init__(self, fields: list[dict[str, str]]) -> None:
        super().__init__("credentials rule violation")
        self.fields = fields


class InvalidCredentials(Exception):
    """없는 아이디와 틀린 비밀번호를 구분하지 않는다 — 응답으로 계정 존재를 알 수 없게."""


class AccountLocked(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("account locked")
        self.retry_after_seconds = retry_after_seconds


class SessionInvalid(Exception):
    """없는·만료된·취소된 세션 토큰."""


@dataclass(frozen=True)
class IssuedSession:
    subject: str
    display_name: str
    # 클라이언트에 한 번만 보여 주는 원문. 로그·메트릭에 넣지 않는다.
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class AuthPolicy:
    signup_enabled: bool
    session_ttl_seconds: int
    login_lock_threshold: int
    login_lock_seconds: int


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def normalize_username(raw: str) -> str:
    # 가입·로그인 모두 같은 정규화를 거쳐야 "Alice"로 가입하고 "alice"로 로그인할 수 있다.
    return raw.lower()


def check_signup_rules(username: str, password: str) -> None:
    """정규화된 username과 password가 가입 규칙에 맞는지 본다. 어긋난 항목을 모두 모아 알린다."""
    fields: list[dict[str, str]] = []
    if not USERNAME_PATTERN.fullmatch(username):
        fields.append({"field": "username", "code": "invalid_format"})
    if not PASSWORD_MIN_CODE_POINTS <= len(password) <= PASSWORD_MAX_CODE_POINTS:
        fields.append({"field": "password", "code": "invalid_length"})
    elif not password.strip():
        fields.append({"field": "password", "code": "blank"})
    if fields:
        raise CredentialsRuleViolation(fields)


class AuthService:
    def __init__(
        self, store: AuthStore, policy: AuthPolicy, passwords: PasswordHashing | None = None
    ) -> None:
        self._store = store
        self._policy = policy
        self._passwords = passwords or PasswordHashing()

    def signup(self, raw_username: str, password: str) -> IssuedSession:
        """새 계정을 만들고 바로 세션을 발급한다.

        가입이 꺼져 있으면 입력을 보기 전에 거절한다(SignupDisabled). 그다음 규칙 위반
        (CredentialsRuleViolation), 마지막으로 중복(store.UsernameTaken) 순서다.
        """
        if not self._policy.signup_enabled:
            raise SignupDisabled
        username = normalize_username(raw_username)
        check_signup_rules(username, password)
        subject = f"{LOCAL_SUBJECT_PREFIX}{uuid4()}"
        token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        expires_at = self._store.create_account(
            subject=subject,
            username=username,
            password_hash=self._passwords.hash(password),
            token_hash=hash_token(token),
            ttl_seconds=self._policy.session_ttl_seconds,
        )
        return IssuedSession(subject, username, token, expires_at)

    def login(self, raw_username: str, password: str) -> IssuedSession:
        """비밀번호를 확인하고 세션을 발급한다. 예외: InvalidCredentials, AccountLocked.

        판정 순서와 이유:
        1. 없는 아이디 → 더미 해시로 verify 1회 후 실패. 있는 아이디와 비슷한 시간을 쓴다.
        2. 잠금 중 → 비밀번호를 확인하지 않고 423. 잠금 중에는 맞는 비밀번호로도 들어오지 못한다.
        3. 비밀번호 확인. 실패면 횟수를 올리고 임계치에 닿으면 잠근다(이번 응답은 401).
        """
        # 로그인에서는 형식 규칙을 따로 검사하지 않는다. 규칙에 맞지 않는 아이디는 애초에 가입될 수
        # 없어 "없는 아이디"로 처리되고, 응답도 같은 401이다.
        username = normalize_username(raw_username)
        # 실패도 기록(실패 횟수·잠금)을 남겨야 하므로, 판정은 트랜잭션 안에서 하되 예외는 블록을
        # 빠져나온 뒤에 던진다. 블록 안에서 던지면 트랜잭션이 롤백되어 실패 횟수가 사라진다.
        with self._store.login_attempt(username) as attempt:
            outcome = self._judge_login(attempt, password)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _judge_login(
        self, attempt: LoginAttempt, password: str
    ) -> IssuedSession | InvalidCredentials | AccountLocked:
        credential = attempt.credential
        if credential is None:
            self._passwords.verify_against_dummy(password)
            return InvalidCredentials()

        if credential.locked_until is not None and credential.locked_until > attempt.now:
            remaining = (credential.locked_until - attempt.now).total_seconds()
            return AccountLocked(retry_after_seconds=max(1, math.ceil(remaining)))

        if not self._passwords.verify(credential.password_hash, password):
            self._record_failure(attempt)
            return InvalidCredentials()

        token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        expires_at = attempt.record_success(hash_token(token), self._policy.session_ttl_seconds)
        return IssuedSession(credential.subject, credential.display_name, token, expires_at)

    def _record_failure(self, attempt: LoginAttempt) -> None:
        credential = attempt.credential
        assert credential is not None  # login()이 없는 아이디를 먼저 걸러낸다.
        # 잠금이 이미 풀렸다면 지난 실패는 잊고 1부터 센다. 그러지 않으면 잠금이 풀린 직후 한 번만
        # 틀려도 바로 다시 잠긴다.
        previous_failures = 0 if credential.locked_until is not None else credential.failed_attempts
        failures = previous_failures + 1
        locked_until = None
        if failures >= self._policy.login_lock_threshold:
            locked_until = attempt.now + timedelta(seconds=self._policy.login_lock_seconds)
        attempt.record_failure(failures, locked_until)

    def authenticate(self, token: str) -> tuple[str, str] | None:
        """유효한 세션 토큰이면 (subject, display_name), 아니면 None.

        sessions 테이블이 아직 없으면(bridge 릴리스가 0005 DB에서 도는 동안) 세션이 존재할 수
        없으므로 None이다. 여기서 503을 내면 정적 토큰이 아닌 잘못된 토큰 하나가 일반 API의
        401을 503으로 바꿔 버린다.
        """
        token_hash = hash_token(token)
        try:
            session = self._store.find_session(token_hash)
        except AuthSchemaNotReady:
            return None
        if session is None or not _is_active(session):
            return None
        if _needs_touch(session):
            self._store.touch_session(token_hash, LAST_SEEN_MIN_INTERVAL_SECONDS)
        return session.subject, session.display_name

    def logout(self, token: str) -> None:
        """세션을 취소한다. 이미 만료·취소됐거나 없는 토큰이면 SessionInvalid."""
        token_hash = hash_token(token)
        session = self._store.find_session(token_hash)
        if session is None or not _is_active(session):
            raise SessionInvalid
        self._store.revoke_session(token_hash)


def _is_active(session: SessionRecord) -> bool:
    return session.revoked_at is None and session.expires_at > session.now


def _needs_touch(session: SessionRecord) -> bool:
    if session.last_seen_at is None:
        return True
    elapsed = (session.now - session.last_seen_at).total_seconds()
    return elapsed >= LAST_SEEN_MIN_INTERVAL_SECONDS
