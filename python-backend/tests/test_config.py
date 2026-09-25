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
