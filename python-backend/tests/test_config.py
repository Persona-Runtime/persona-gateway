"""Settings 검증 — llm 모드의 연결 설정이 기동 시점에 강제되는지.

값이 빠진 채로 뜨면 운영자는 "기동은 됐는데 채팅만 안 된다"를 첫 요청에서야 본다.
반대로 mock 모드에 새 env를 강제하면 기존 기동·테스트가 전부 깨진다 — 두 경우를 함께 본다.
"""

from __future__ import annotations

import pytest

from persona_minimal_api.config import Settings

# 합성 값만 쓴다.
BASE_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql://unused",
    "PERSONA_EMBEDDING_URL": "http://embedding.invalid",
    "PERSONA_STATIC_BEARER_TOKEN": "synthetic-token",
    "PERSONA_STATIC_USER_ID": "synthetic-user",
    "PERSONA_STATIC_DISPLAY_NAME": "합성 사용자",
    "PERSONA_CURSOR_SIGNING_KEY": "synthetic-cursor-key",
}


def test_mock_mode_needs_no_vllm_settings_and_is_the_default() -> None:
    settings = Settings(**BASE_ENV)

    assert settings.chat_inference_mode == "mock"
    assert settings.vllm_base_url is None
    assert settings.vllm_model is None


def test_llm_mode_without_base_url_fails_at_startup() -> None:
    with pytest.raises(ValueError) as raised:
        Settings(
            **BASE_ENV,
            PERSONA_CHAT_INFERENCE_MODE="llm",
            PERSONA_VLLM_MODEL="synthetic-model",
        )

    assert "PERSONA_VLLM_BASE_URL" in str(raised.value)


def test_llm_mode_without_model_fails_at_startup() -> None:
    with pytest.raises(ValueError) as raised:
        Settings(
            **BASE_ENV,
            PERSONA_CHAT_INFERENCE_MODE="llm",
            PERSONA_VLLM_BASE_URL="http://vllm.invalid:8000",
        )

    assert "PERSONA_VLLM_MODEL" in str(raised.value)


def test_llm_mode_with_complete_settings_is_accepted() -> None:
    settings = Settings(
        **BASE_ENV,
        PERSONA_CHAT_INFERENCE_MODE="llm",
        PERSONA_VLLM_BASE_URL="http://vllm.invalid:8000",
        PERSONA_VLLM_MODEL="synthetic-model",
    )

    assert settings.chat_inference_mode == "llm"
    assert settings.vllm_api_key is None  # --api-key 없이 띄운 vLLM이 기본이다.


def test_unknown_inference_mode_is_rejected() -> None:
    # 모르는 값을 기본값(mock)으로 눙치면 합성 응답이 진짜 답변으로 저장된다.
    with pytest.raises(ValueError):
        Settings(**BASE_ENV, PERSONA_CHAT_INFERENCE_MODE="vllm")


def test_vllm_timeouts_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Settings(
            **BASE_ENV,
            PERSONA_CHAT_INFERENCE_MODE="llm",
            PERSONA_VLLM_BASE_URL="http://vllm.invalid:8000",
            PERSONA_VLLM_MODEL="synthetic-model",
            PERSONA_VLLM_IDLE_TIMEOUT_SECONDS="0",
        )


def test_generation_lease_defaults_keep_the_safety_margin() -> None:
    settings = Settings(**BASE_ENV)

    assert settings.generation_heartbeat_seconds == 10.0
    assert settings.generation_lease_seconds == 30.0
    assert settings.generation_lease_seconds >= (
        2 * settings.generation_heartbeat_seconds + settings.database_timeout_seconds
    )


def test_lease_shorter_than_two_heartbeats_plus_db_timeout_fails_at_startup() -> None:
    # 10 × 2 + 2 = 22 > 20 — 연장 한 번이 늦으면 살아 있는 소유자의 lease가 만료될 수 있다.
    with pytest.raises(ValueError) as raised:
        Settings(
            **BASE_ENV,
            PERSONA_GENERATION_HEARTBEAT_SECONDS="10",
            PERSONA_GENERATION_LEASE_SECONDS="20",
        )

    assert "PERSONA_GENERATION_LEASE_SECONDS" in str(raised.value)


@pytest.mark.parametrize(
    "env_name", ["PERSONA_GENERATION_HEARTBEAT_SECONDS", "PERSONA_GENERATION_LEASE_SECONDS"]
)
def test_lease_settings_must_be_positive(env_name: str) -> None:
    with pytest.raises(ValueError):
        Settings(**BASE_ENV, **{env_name: "0"})


def test_mock_profile_defaults_to_short() -> None:
    assert Settings(**BASE_ENV).chat_mock_profile == "short"


def test_unknown_mock_profile_fails_at_startup_without_echoing_input() -> None:
    with pytest.raises(ValueError) as raised:
        Settings(**BASE_ENV, PERSONA_CHAT_MOCK_PROFILE="synthetic-unknown-profile")

    assert "PERSONA_CHAT_MOCK_PROFILE" in str(raised.value)
    assert "synthetic-unknown-profile" not in str(raised.value)


def _recorded_mock_profile() -> str | None:
    from prometheus_client import REGISTRY

    for metric in REGISTRY.collect():
        if metric.name == "persona_chat_mock_workload":
            return metric.samples[0].labels["profile"]
    return None


def test_mock_profile_selects_fake_client_shape_only_in_mock_mode() -> None:
    from persona_minimal_api.chat.fake_inference import FakeInferenceClient
    from persona_minimal_api.chat.vllm_client import VllmInferenceClient
    from persona_minimal_api.main import build_inference_client

    # mock: 설정한 profile이 적용·기록된다(조각을 실제로 흘려 보내지 않는다 — 시간 의존 회피).
    mock_client = build_inference_client(Settings(**BASE_ENV, PERSONA_CHAT_MOCK_PROFILE="medium"))
    assert isinstance(mock_client, FakeInferenceClient)
    assert _recorded_mock_profile() == "medium"

    llm_settings = Settings(
        **BASE_ENV,
        PERSONA_CHAT_INFERENCE_MODE="llm",
        PERSONA_VLLM_BASE_URL="http://vllm.invalid",
        PERSONA_VLLM_MODEL="synthetic-model",
        PERSONA_CHAT_MOCK_PROFILE="long",
    )
    llm_client = build_inference_client(llm_settings)
    try:
        # llm 모드는 profile을 읽지 않는다 — vLLM adapter를 쓰고, mock profile 기록도 바꾸지 않는다.
        assert isinstance(llm_client, VllmInferenceClient)
        assert _recorded_mock_profile() == "medium"
    finally:
        llm_client.close()
