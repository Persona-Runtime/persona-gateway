from __future__ import annotations

import pytest

from persona_minimal_api.chat.service import InvalidQuestion, validate_question


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
