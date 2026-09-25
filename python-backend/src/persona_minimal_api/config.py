from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # hide_input_in_errors: 설정 검증 실패 메시지(기동 로그)에 입력값을 싣지 않는다 —
    # vLLM URL·DB URL처럼 내부 주소가 담긴 값이 오류 메시지로 새어 나가지 않게 한다.
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)

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
    # mock: FakeInferenceClient(모델 호출 없음), llm: vLLM OpenAI 호환 streaming API.
    # 기본값은 mock이다. llm 연결이 실패해도 mock으로 조용히 fallback하지 않는다 —
    # 어떤 client를 쓸지는 main.build_inference_client()가 이 값 하나로만 정한다.
    # Literal이라 두 값 외에는 pydantic이 기동 시점에 바로 거부한다.
    chat_inference_mode: Literal["mock", "llm"] = Field(
        default="mock", validation_alias="PERSONA_CHAT_INFERENCE_MODE"
    )
    # 아래 vLLM 설정은 llm 모드에서만 쓴다. mock 모드 기동·테스트에 새 연결 설정을
    # 강제하지 않으려고 모두 선택값으로 두고, llm일 때의 필수 여부는
    # llm_settings_are_complete가 검사한다.
    # base URL은 "/v1" 앞부분까지다(예: http://vllm:8000). 경로는 adapter가 붙인다.
    vllm_base_url: str | None = Field(default=None, validation_alias="PERSONA_VLLM_BASE_URL")
    # OpenAI 요청의 "model" 필드 — vLLM의 served-model-name과 같아야 한다.
    vllm_model: str | None = Field(default=None, validation_alias="PERSONA_VLLM_MODEL")
    # vLLM을 --api-key로 띄웠을 때만 준다. 없으면 Authorization 헤더를 보내지 않는다.
    vllm_api_key: SecretStr | None = Field(default=None, validation_alias="PERSONA_VLLM_API_KEY")
    # TCP 연결 수립까지의 한도(초).
    vllm_connect_timeout_seconds: float = Field(
        default=5.0, validation_alias="PERSONA_VLLM_CONNECT_TIMEOUT_SECONDS"
    )
    # 요청 전송부터 첫 content 조각까지의 한도(초). keepalive·빈 delta는 시간을 늘려주지
    # 않는다. 서비스 계층의 60초 first_token_timeout(검색 시간 포함, 접수 기준)과 별개로
    # upstream 호출만 따로 잰다.
    vllm_first_token_timeout_seconds: float = Field(
        default=60.0, validation_alias="PERSONA_VLLM_FIRST_TOKEN_TIMEOUT_SECONDS"
    )
    # 첫 조각 이후 content 조각 사이의 최대 간격(초).
    vllm_idle_timeout_seconds: float = Field(
        default=30.0, validation_alias="PERSONA_VLLM_IDLE_TIMEOUT_SECONDS"
    )
    # generation 소유권 lease(G-1). 살아 있는 인스턴스는 heartbeat 간격마다 자기 활성
    # generation의 lease를 lease 길이만큼 늘리고, 다른 인스턴스는 lease가 지난 행만 회수한다.
    # 두 값의 관계는 generation_lease_is_safe가 검사한다. 기본값 10초·30초의 의미: 연장이
    # 한 번 실패하고 DB가 timeout까지 느려도(10 + 10 + 2 < 30) 살아 있는 소유자의 행이
    # 만료되지 않는다. OOM·노드 장애 뒤 회수까지는 최대 lease 길이(30초)가 걸린다. graceful
    # shutdown(uvicorn 25초) 동안에도 heartbeat는 계속 돌고, lifespan 종료에서 멈춘다.
    generation_heartbeat_seconds: float = Field(
        default=10.0, validation_alias="PERSONA_GENERATION_HEARTBEAT_SECONDS"
    )
    generation_lease_seconds: float = Field(
        default=30.0, validation_alias="PERSONA_GENERATION_LEASE_SECONDS"
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

    @field_validator(
        "database_timeout_seconds",
        "vllm_connect_timeout_seconds",
        "vllm_first_token_timeout_seconds",
        "vllm_idle_timeout_seconds",
        "generation_heartbeat_seconds",
        "generation_lease_seconds",
    )
    @classmethod
    def timeout_is_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be positive")
        return value

    @model_validator(mode="after")
    def generation_lease_is_safe(self) -> Settings:
        """lease는 heartbeat 두 번과 DB timeout을 합한 것보다 길어야 한다.

        짧으면 연장 한 번이 늦거나 실패하는 것만으로 살아 있는 소유자의 lease가 만료되고,
        다른 인스턴스가 정상 스트림을 reconciling으로 회수한다 — G-1이 막으려던 바로 그 문제다.
        """
        minimum = 2 * self.generation_heartbeat_seconds + self.database_timeout_seconds
        if self.generation_lease_seconds < minimum:
            raise ValueError(
                "PERSONA_GENERATION_LEASE_SECONDS must be >= 2 * "
                "PERSONA_GENERATION_HEARTBEAT_SECONDS + PERSONA_DB_TIMEOUT_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def llm_settings_are_complete(self) -> Settings:
        """llm 모드인데 연결 설정이 비어 있으면 기동을 실패시킨다.

        첫 채팅 요청에서야 드러나게 두면 운영자는 "기동은 됐는데 채팅만 안 된다"를 보게
        된다. 오류 메시지에는 어떤 env가 빠졌는지만 적고 값은 적지 않는다.
        """
        if self.chat_inference_mode != "llm":
            return self
        missing = [
            env_name
            for env_name, value in (
                ("PERSONA_VLLM_BASE_URL", self.vllm_base_url),
                ("PERSONA_VLLM_MODEL", self.vllm_model),
            )
            if value is None or not value.strip()
        ]
        if missing:
            raise ValueError(f"llm inference mode requires {', '.join(missing)}")
        return self
