from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .retrieval.prompt import DEFAULT_PROMPT_VERSION, PromptVersion


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
    # mock 모드의 응답 모양(chat/fake_inference.MOCK_WORKLOAD_PROFILES). 서버 설정으로만
    # 고른다 — 요청 body·query·header로 바꾸는 경로는 없다(사용자가 스트림 길이를 조작해
    # 슬롯을 오래 쥐거나 실험 결과를 흔들 수 없게). llm 모드에서는 읽지 않는다. 기본값
    # short는 기존 기본 mock과 같은 응답이다. Literal이라 다른 값이면 기동이 실패한다.
    chat_mock_profile: Literal["short", "medium", "long"] = Field(
        default="short", validation_alias="PERSONA_CHAT_MOCK_PROFILE"
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

    # --- 계정·세션 인증(A-1) ---
    # 정적 Bearer 토큰 경로를 계속 받을지. 웹·실험 스크립트가 세션 토큰으로 전환된 뒤 platform이
    # false로 내린다. 꺼도 PERSONA_STATIC_BEARER_TOKEN 등 기존 env는 그대로 필수다(기동 계약 유지).
    static_token_enabled: bool = Field(
        default=True, validation_alias="PERSONA_STATIC_TOKEN_ENABLED"
    )
    # 세션 수명(초). 발급 시각에 고정하고 활동으로 연장하지 않는다 — 탈취된 토큰의 유효 기간에
    # 상한을 두기 위해서다. 기본 7일.
    session_ttl_seconds: int = Field(default=604800, validation_alias="PERSONA_SESSION_TTL_SECONDS")
    # 연속 로그인 실패가 이 횟수에 닿으면 계정을 login_lock_seconds 동안 잠근다.
    login_lock_threshold: int = Field(default=5, validation_alias="PERSONA_LOGIN_LOCK_THRESHOLD")
    login_lock_seconds: int = Field(default=900, validation_alias="PERSONA_LOGIN_LOCK_SECONDS")
    # 공개 회원가입 on/off. 초대 코드 없이 열어 둔다(소유자 결정). 끄면 signup은 403.
    signup_enabled: bool = Field(default=True, validation_alias="PERSONA_SIGNUP_ENABLED")

    @field_validator("session_ttl_seconds", "login_lock_threshold", "login_lock_seconds")
    @classmethod
    def auth_limit_is_positive(cls, value: int) -> int:
        # 0 이하는 "발급 즉시 만료"·"잠금 시간 없음"처럼 기능이 조용히 깨진 설정이라 기동에서 거부한다.
        if value <= 0:
            raise ValueError("must be positive")
        return value

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

    # --- 생성 품질 (Q-1) ---------------------------------------------------
    # 프롬프트 버전과 vLLM 샘플링 값. 전부 서버 설정으로만 고른다(요청으로 바꾸는 경로 없음).
    # 골든셋(scripts/quality-eval)이 같은 코드로 v1/v2·샘플링 조합을 비교하려고 env로 뺐다.
    # 샘플링 값은 llm 모드에서만 upstream에 실리고 mock 모드는 읽기만 한다.
    prompt_version: PromptVersion = Field(
        default=DEFAULT_PROMPT_VERSION, validation_alias="PERSONA_PROMPT_VERSION"
    )
    # 기본값 출처: Qwen3-4B-Instruct-2507 모델 카드 권장값 temperature 0.7 · top_p 0.8 ·
    # top_k 20 · min_p 0. presence_penalty는 권장 범위 0~2인데 1.5 이상에서 언어 섞임 보고가
    # 있어 1.0에서 시작한다. repetition_penalty 1.0은 "끔"과 같다.
    vllm_temperature: float = Field(default=0.7, validation_alias="PERSONA_VLLM_TEMPERATURE")
    vllm_top_p: float = Field(default=0.8, validation_alias="PERSONA_VLLM_TOP_P")
    vllm_top_k: int = Field(default=20, validation_alias="PERSONA_VLLM_TOP_K")
    vllm_min_p: float = Field(default=0.0, validation_alias="PERSONA_VLLM_MIN_P")
    vllm_presence_penalty: float = Field(
        default=1.0, validation_alias="PERSONA_VLLM_PRESENCE_PENALTY"
    )
    vllm_repetition_penalty: float = Field(
        default=1.0, validation_alias="PERSONA_VLLM_REPETITION_PENALTY"
    )

    @model_validator(mode="after")
    def sampling_is_in_range(self) -> Settings:
        """vLLM이 거부하는 샘플링 값을 기동 시점에 막는다.

        잘못된 값을 그대로 두면 기동은 되고 모든 채팅 요청만 upstream 400으로 실패한다.
        범위는 vLLM SamplingParams 검증과 같다. 오류에는 env 이름만 적고 값은 적지 않는다.
        """
        invalid = [
            env_name
            for env_name, is_valid in (
                ("PERSONA_VLLM_TEMPERATURE", self.vllm_temperature >= 0),
                ("PERSONA_VLLM_TOP_P", 0 < self.vllm_top_p <= 1),
                # -1은 vLLM에서 "top_k 끔"이다. 0은 거부된다.
                ("PERSONA_VLLM_TOP_K", self.vllm_top_k == -1 or self.vllm_top_k >= 1),
                ("PERSONA_VLLM_MIN_P", 0 <= self.vllm_min_p <= 1),
                ("PERSONA_VLLM_PRESENCE_PENALTY", -2 <= self.vllm_presence_penalty <= 2),
                ("PERSONA_VLLM_REPETITION_PENALTY", self.vllm_repetition_penalty > 0),
            )
            if not is_valid
        ]
        if invalid:
            raise ValueError(f"sampling settings out of range: {', '.join(invalid)}")
        return self
