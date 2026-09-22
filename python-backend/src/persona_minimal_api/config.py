from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str = Field(validation_alias="DATABASE_URL")
    # database_url과 같은 성격 — 반드시 외부에서 줘야 하는 연결 대상이라 기본값을
    # 두지 않는다(빈 문자열이라도 호출 시점에야 실패가 드러나는 게, 조용한 기본값보다
    # 낫다는 판단도 database_url과 같다).
    embedding_url: str = Field(validation_alias="PERSONA_EMBEDDING_URL")
    static_bearer_token: SecretStr = Field(validation_alias="PERSONA_STATIC_BEARER_TOKEN")
    static_user_id: str = Field(validation_alias="PERSONA_STATIC_USER_ID")
    static_display_name: str = Field(validation_alias="PERSONA_STATIC_DISPLAY_NAME")
    cursor_signing_key: SecretStr = Field(validation_alias="PERSONA_CURSOR_SIGNING_KEY")
    database_timeout_seconds: float = Field(
        default=2.0, validation_alias="PERSONA_DB_TIMEOUT_SECONDS"
    )
    # /retrieve 응답에 원문 조각이 그대로 들어간다 — 기본은 꺼둔다. platform prod
    # overlay에는 이 env를 넣지 않는다(=off로 유지).
    retrieve_debug_enabled: bool = Field(
        default=False, validation_alias="PERSONA_RETRIEVE_DEBUG_ENABLED"
    )
    # Traefik의 oauth-forward Middleware가 세팅하는 X-Auth-Request-User를 신원으로
    # 받아들일지 여부 — 기본은 꺼둔다. 이 값이 켜져도 신뢰 근거(NetworkPolicy·
    # ForwardAuth authResponseHeaders 덮어쓰기·strip-auth-header)가 전제다.
    # authenticated_user 문서화 참고. platform prod overlay엔 Gate 4가 아직
    # 클러스터 미적용이라 이번에도 넣지 않는다(=off로 유지).
    forward_auth_enabled: bool = Field(
        default=False, validation_alias="PERSONA_FORWARD_AUTH_ENABLED"
    )
    # 값 도메인이 지금은 "mock" 하나뿐이다(vLLM 어댑터가 아직 없다) — 그래도 명시적
    # 설정값으로 강제해 둔다. "llm"을 나중에 추가할 때, 연결 실패 시 조용히 mock으로
    # fallback하는 경로가 생기지 않게 하려는 목적이다(feedback.md 명시 요구). Literal이라
    # "mock" 외의 값은 pydantic이 기동 시점에 바로 거부한다 — 조용한 fallback 대신
    # 시끄러운 실패.
    chat_inference_mode: Literal["mock"] = Field(
        default="mock", validation_alias="PERSONA_CHAT_INFERENCE_MODE"
    )

    @field_validator("static_user_id", "static_display_name")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("cursor_signing_key")
    @classmethod
    def cursor_key_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 16:
            raise ValueError("must be at least 16 characters")
        return value

    @field_validator("database_timeout_seconds")
    @classmethod
    def database_timeout_is_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be positive")
        return value
