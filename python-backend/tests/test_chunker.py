"""§4-2 청킹 규칙 골든 테스트. 합성 텍스트만 쓴다."""

from __future__ import annotations

import pytest

from persona_minimal_api.indexing.chunker import (
    MAX_CHARS,
    MIN_CHARS,
    TARGET_CHARS,
    _merge_below_floor,
    chunk_material,
)


def test_rejects_kind_that_is_not_indexed() -> None:
    # profile은 §4-3 계약상 색인 대상이 아니다 — 항상 프롬프트에 통째로 들어간다.
    with pytest.raises(ValueError, match="profile"):
        chunk_material("아무 텍스트", "profile")


def test_heading_path_tracks_two_and_three_level_hierarchy() -> None:
    md = (
        "# 개요\n\n첫 문단이다.\n\n"
        "## 세부\n\n둘째 문단이다.\n\n"
        "### 더 깊은 절\n\n셋째 문단이다.\n\n"
        "## 다른 절\n\n넷째 문단이다.\n"
    )

    chunks = chunk_material(md, "events")

    assert [c.heading_path for c in chunks] == [
        ("개요",),
        ("개요", "세부"),
        ("개요", "세부", "더 깊은 절"),
        ("개요", "다른 절"),
    ]
    assert [c.content for c in chunks] == [
        "[개요] 첫 문단이다.",
        "[개요 > 세부] 둘째 문단이다.",
        "[개요 > 세부 > 더 깊은 절] 셋째 문단이다.",
        "[개요 > 다른 절] 넷째 문단이다.",
    ]
    assert [c.ordinal for c in chunks] == [0, 1, 2, 3]


def test_long_paragraph_splits_at_sentence_boundary_near_target_and_under_cap() -> None:
    sentence = "이것은 합성 테스트 문장이다."  # 각주·문장부호 없는 단순 반복 단위
    paragraph = " ".join([sentence] * 50)
    md = f"# 제목\n\n{paragraph}\n"

    chunks = chunk_material(md, "events")

    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        # 목표(TARGET_CHARS) 근처에서 끊되, 상한(MAX_CHARS)은 절대 넘지 않는다.
        assert TARGET_CHARS <= chunk.char_count <= MAX_CHARS
    # 어느 조각도 문장 중간에서 잘리지 않는다 — 이어 붙이면 원문과 같아야 한다.
    rejoined = " ".join(chunk.content.split("] ", 1)[1] for chunk in chunks)
    assert rejoined == paragraph


def test_short_paragraph_merges_into_the_following_paragraph_below_floor() -> None:
    md = "# 제목\n\n짧다.\n\n뒤따르는 문단이 이어서 합쳐진다.\n"

    chunks = chunk_material(md, "abilities")

    # 첫 문단("짧다.")은 하한(120자) 미만이라 독립 조각이 되지 않고 다음 문단에 붙는다.
    assert len(chunks) == 1
    assert chunks[0].content == "[제목] 짧다. 뒤따르는 문단이 이어서 합쳐진다."


def test_trailing_short_paragraph_merges_backward_when_no_next_paragraph_exists() -> None:
    long_sentence = "이것은 충분히 긴 합성 문장이다 " * 10  # 120자 이상 확보
    md = f"# 제목\n\n{long_sentence.strip()}.\n\n짧다.\n"

    chunks = chunk_material(md, "abilities")

    assert len(chunks) == 1
    assert chunks[0].content.endswith("짧다.")
    assert chunks[0].char_count >= MIN_CHARS


def test_footnote_marker_stays_attached_to_its_sentence_across_the_boundary() -> None:
    md = "# 개요\n\n위기에도 냉정함을 유지한다.[^1] 그 점이 다르다.\n"

    (chunk,) = chunk_material(md, "events")

    assert "유지한다.[^1] 그 점이 다르다." in chunk.content


def test_footnote_section_is_chunked_like_any_other_heading() -> None:
    md = "# 개요\n\n본문이다.\n\n## 각주\n\n[^1]: 각주 설명이다.\n"

    chunks = chunk_material(md, "events")

    footnote_chunk = next(c for c in chunks if c.heading_path == ("개요", "각주"))
    assert footnote_chunk.content == "[개요 > 각주] [^1]: 각주 설명이다."


def test_speech_examples_is_line_based_and_does_not_truncate_long_lines() -> None:
    long_line = "합성 인물: " + "긴 대사 " * 200  # 상한(MAX_CHARS)보다 훨씬 길게
    text = f"합성 인물: 안녕\n\n{long_line}\n\n합성 인물: 잘 가\n"

    chunks = chunk_material(text, "speech_examples")

    assert [c.heading_path for c in chunks] == [(), (), ()]
    assert chunks[1].content == long_line.strip()
    assert chunks[1].char_count > MAX_CHARS  # 상한 예외 — 줄은 자르지 않는다
    assert [c.ordinal for c in chunks] == [0, 1, 2]


def test_same_input_produces_the_same_chunks() -> None:
    md = "# 개요\n\n첫 문단이다.\n\n## 세부\n\n둘째 문단이다.\n"

    assert chunk_material(md, "events") == chunk_material(md, "events")


def test_list_without_punctuation_still_splits_within_cap() -> None:
    # 나무위키식 목록·표 행은 마침표가 없다. 줄 단위 경계가 없으면 blank line이 없는 한
    # 문단 전체가 "문장 하나"로 뭉쳐 상한을 넘는 조각이 그대로 나간다.
    lines = "\n".join(
        f"- 항목 {i}: 이 목록 줄은 마침표가 없는 합성 설명 텍스트다" for i in range(20)
    )
    md = f"# 개요\n\n{lines}\n"

    chunks = chunk_material(md, "events")

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.char_count <= MAX_CHARS


def test_floor_merge_keeps_cap_priority_over_floor() -> None:
    # 합치면 상한을 넘는 경우, 하한 미만이어도 합치지 않고 그대로 독립시킨다.
    long_piece = "가" * (MAX_CHARS - 30)
    short_piece = "나" * (MIN_CHARS - 1)

    assert _merge_below_floor([long_piece, short_piece]) == [long_piece, short_piece]
