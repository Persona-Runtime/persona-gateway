from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from persona_minimal_api.chat import service
from persona_minimal_api.chat.fake_inference import FakeInferenceClient
from persona_minimal_api.chat.repository import Generation, InputSnapshot
from persona_minimal_api.chat.service import (
    InvalidQuestion,
    prompt_budget_for_mode,
    validate_question,
)
from persona_minimal_api.retrieval.prompt import BUDGET_4096, BUDGET_8192, system_instruction_for


def test_validate_question_strips_and_accepts_normal_text() -> None:
    assert validate_question("  안녕하세요  ") == "안녕하세요"


def test_validate_question_rejects_blank() -> None:
    with pytest.raises(InvalidQuestion) as excinfo:
        validate_question("   ")
    assert excinfo.value.code == "blank"


def test_validate_question_rejects_over_2000_codepoints() -> None:
    with pytest.raises(InvalidQuestion) as excinfo:
        validate_question("가" * 2001)
    assert excinfo.value.code == "too_long"


def test_validate_question_accepts_exactly_2000_codepoints() -> None:
    question = "가" * 2000
    assert validate_question(question) == question


def test_llm_mode_uses_4096_prompt_budget() -> None:
    # 운영 vLLM의 --max-model-len 4096에 맞춘 글자 수 예산이다(토큰 보장은 아니다).
    assert prompt_budget_for_mode("llm") is BUDGET_4096


def test_mock_mode_keeps_8192_prompt_budget() -> None:
    assert prompt_budget_for_mode("mock") is BUDGET_8192


# --- 프롬프트 버전 전달 (Q-1) ------------------------------------------------


class _RecordingChatStore:
    """_run_generation이 부르는 ChatStore 메서드만 흉내 낸다. DB 없이 조립된 프롬프트를 본다."""

    def __init__(self) -> None:
        self.snapshot: InputSnapshot | None = None

    def load_history(self, conversation_id: UUID, *, before_user_message_id: UUID):
        del conversation_id, before_user_message_id
        return []

    def mark_generation_running(self, generation_id: UUID, snapshot: InputSnapshot) -> None:
        del generation_id
        self.snapshot = snapshot

    def finish_generation(
        self, generation_id: UUID, *, status: str, content: str, failure_code: str | None
    ) -> Generation:
        del generation_id
        return replace(_GENERATION, status=status, content=content, failure_code=failure_code)


_GENERATION = Generation(
    id=uuid4(),
    conversation_id=uuid4(),
    user_message_id=uuid4(),
    version_id=uuid4(),
    mode="mock",
    status="running",
    content="",
    citations=[],
    failure_code=None,
    retry_of_generation_id=None,
    input_snapshot=None,
    created_at=datetime.now(timezone.utc),
    finished_at=None,
)


@pytest.mark.parametrize("prompt_version", ["v1", "v2"])
def test_generation_assembles_the_prompt_with_the_client_prompt_version(
    monkeypatch: pytest.MonkeyPatch, prompt_version: str
) -> None:
    # 준비: 검색·설정 조회(DB·임베딩)를 합성 값으로 바꾼다.
    monkeypatch.setattr(
        service,
        "retrieve_context",
        lambda *args, **kwargs: SimpleNamespace(speech=[], body=[]),
    )
    monkeypatch.setattr(service, "_version_settings", lambda pool, version_id: ("민서", "합성"))
    chat_store = _RecordingChatStore()
    client = FakeInferenceClient(chunks=("응",), prompt_version=prompt_version)

    # 실행
    events = list(
        service._run_generation(
            pool=None,  # type: ignore[arg-type]  # retrieve_context를 바꿨으므로 쓰이지 않는다
            embedding_url="http://embedding.invalid",
            chat_store=chat_store,  # type: ignore[arg-type]  # 필요한 메서드만 가진 가짜
            owner_subject="synthetic-user",
            persona_id=uuid4(),
            generation=_GENERATION,
            question="합성 질문",
            inference_client=client,
            first_token_deadline_seconds=5.0,
            total_deadline_seconds=5.0,
            start=time.monotonic(),
            client_gone=None,
        )
    )

    # 검증: 저장된 프롬프트의 system 지시가 client 버전의 것이다.
    assert events[-1].startswith("event: done")
    assert chat_store.snapshot is not None
    system_content = chat_store.snapshot.messages[0].content
    assert system_content.startswith(system_instruction_for(prompt_version, "민서"))
