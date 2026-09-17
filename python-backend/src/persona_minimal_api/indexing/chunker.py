"""입력 마크다운을 검색용 조각(Chunk)으로 나눈다 — 인수인계 §4-2.

순수 함수다. DB·임베딩·네트워크를 모르고, 같은 입력이면 항상 같은 조각 목록을 낸다
(딕셔너리 순서·해시 같은 비결정 요소를 쓰지 않는다). `store.py`가 `id`·`source_id`·
`embedding`을 붙이는 건 이 모듈의 책임이 아니다.

입력은 corpus-tools의 §4-1 붙여넣기 출력과 같은 모양을 가정한다: 본문은 `#`~`###`
제목(`persona-corpus-tools`의 `render_wiki_markdown`이 만드는 그대로), 각주는 `[^n]`
마커와 끝의 `## 각주` 절. `speech_examples`만 예외로 마크다운이 아니라 한 줄에 한
대사다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# §4-2가 정한 값. 실측 없이 바꾸지 않는다.
#
# 900자였던 상한은 e5-small의 512토큰 입력 한도를 넘었다(900자 ≈ 530토큰, 1.7자/토큰,
# `passage: ` 접두까지 더하면 더 넘는다). §4-2 자체를 550/800으로 낮췄다.
TARGET_CHARS = 550
MAX_CHARS = 800
MIN_CHARS = 120
_HEADING_PATH_SEPARATOR = " > "

# repository.DRAFT_KINDS에서 profile을 뺀 것과 같아야 한다 — profile은 색인하지 않고
# 항상 프롬프트에 통째로 들어간다(§4-3). 그 제약을 이 모듈 안에서도 강제한다.
CHUNKABLE_KINDS = ("events", "relationships", "abilities", "speech_examples")

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
# 마침표·물음표·느낌표 뒤 공백이 문장 경계다. 그 사이에 각주 마커([^n])가 끼어 있으면
# (예: "…이다.[^1] 다음") 마커까지 앞 문장에 붙이고 그 뒤 공백만 경계로 삼는다. 마커
# 폭이 다양해 파이썬 re의 고정폭 lookbehind로는 표현이 안 돼 finditer로 직접 자른다.
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?](?:\[\^[^\]]+\])*\s+")


@dataclass(frozen=True)
class Chunk:
    """§4-3 material_chunks 행 중 이 모듈이 결정하는 필드만 담는다."""

    kind: str
    ordinal: int
    heading_path: tuple[str, ...]
    content: str
    char_count: int


def chunk_material(text: str, kind: str) -> list[Chunk]:
    """`text`(한 소스의 원문)를 `kind` 규칙에 따라 조각낸다."""
    if kind not in CHUNKABLE_KINDS:
        raise ValueError(f"chunk_material은 kind={kind!r}를 지원하지 않는다: {CHUNKABLE_KINDS}")
    if kind == "speech_examples":
        return _chunk_speech_lines(text)
    return _chunk_markdown_body(text, kind)


def _chunk_speech_lines(text: str) -> list[Chunk]:
    # 줄 단위가 조각이다. 600자 넘는 줄도 자르지 않는다(§4-2 상한 예외) — 그 줄 자체가
    # 이미 하나의 완결된 대사이므로 잘라내면 의미가 끊긴다.
    chunks: list[Chunk] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        chunks.append(Chunk("speech_examples", len(chunks), (), stripped, len(stripped)))
    return chunks


def _chunk_markdown_body(text: str, kind: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    for heading_path, body in _split_by_heading(text):
        # 접두를 먼저 만들어 그 길이를 예산에서 뺀다 — 접두는 조각마다 붙는데 예산
        # 검사가 이걸 모르면 [heading] 붙인 뒤 실제 char_count가 상한을 넘어선다.
        prefix = f"[{_HEADING_PATH_SEPARATOR.join(heading_path)}] " if heading_path else ""
        reserve = len(prefix)
        pieces: list[str] = []
        for paragraph in _split_paragraphs(body):
            pieces.extend(_split_sentences_to_budget(paragraph, reserve=reserve))
        for content in _merge_below_floor(pieces, reserve=reserve):
            prefixed = f"{prefix}{content}"
            chunks.append(Chunk(kind, len(chunks), heading_path, prefixed, len(prefixed)))
    return chunks


def _split_by_heading(text: str) -> list[tuple[tuple[str, ...], str]]:
    """`#`~`###` 줄로 heading_path 스택을 갱신하며 구간별 본문을 모은다."""
    sections: list[tuple[tuple[str, ...], str]] = []
    path: list[str] = []
    body_lines: list[str] = []

    def flush() -> None:
        body = "\n".join(body_lines).strip()
        if body:
            sections.append((tuple(path), body))
        body_lines.clear()

    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            flush()
            level = len(match.group(1))
            # 상위 레벨로 되돌아가면 그 아래 쌓인 제목은 더 이상 현재 경로가 아니다.
            path[:] = path[: level - 1]
            path.append(match.group(2))
            continue
        body_lines.append(line)
    flush()
    return sections


def _split_paragraphs(body: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]


def _split_sentences(paragraph: str) -> list[str]:
    """문단을 문장 단위로 자른다.

    줄바꿈 자체도 경계로 본다 — 나무위키식 목록(`- 네즈코: 여동생`)이나 표 행
    (`| 이름 | 설명 |`)은 마침표가 없어서, 줄 단위로 먼저 안 나누면 blank line이 없는
    한 문단 전체가 "문장 하나"로 뭉쳐 상한을 넘는 조각이 그대로 나간다. 줄 안에서는
    기존처럼 구두점 경계를 추가로 적용한다.
    """
    sentences: list[str] = []
    for line in paragraph.splitlines():
        stripped = line.strip()
        if stripped:
            sentences.extend(_split_line_by_punctuation(stripped))
    return sentences


def _split_line_by_punctuation(line: str) -> list[str]:
    """한 줄 안에서 마침표·물음표·느낌표 뒤 공백을 경계로 자른다.

    경계의 공백만 버리고, 구두점·각주 마커([^n])는 앞 문장에 남긴다.
    """
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_BOUNDARY_RE.finditer(line):
        # match는 "구두점(+각주 마커)+공백" 전체다. 끝의 공백만 잘라 경계로 쓴다.
        end_of_sentence = match.end() - (len(match.group()) - len(match.group().rstrip()))
        sentence = line[start:end_of_sentence]
        if sentence:
            sentences.append(sentence)
        start = match.end()
    tail = line[start:]
    if tail:
        sentences.append(tail)
    return sentences


def _split_sentences_to_budget(paragraph: str, reserve: int = 0) -> list[str]:
    """문장 경계에서만 자르고 붙여 목표·상한에 채운다.

    `reserve`는 나중에 붙는 `[heading > path] ` 접두의 길이다 — 접두를 붙인 뒤의
    실제 길이가 상한을 넘지 않아야 하므로, 여기서는 `MAX_CHARS - reserve`를 상한으로
    쓴다(목표도 같은 비율로 줄인다). 문장 하나가 이미 그 상한을 넘으면(계약에 대비책이
    없는 예외적 입력) 그 문장은 자르지 않고 그대로 하나의 조각으로 낸다 — 문장 중간을
    자르는 것보다는 상한 초과가 낫다.
    """
    effective_target = TARGET_CHARS - reserve
    effective_max = MAX_CHARS - reserve
    sentences = _split_sentences(paragraph)
    pieces: list[str] = []
    buffer = ""
    for sentence in sentences:
        candidate = f"{buffer} {sentence}".strip() if buffer else sentence
        if buffer and len(candidate) > effective_max:
            pieces.append(buffer)
            buffer = sentence
        else:
            buffer = candidate
            if len(buffer) >= effective_target:
                pieces.append(buffer)
                buffer = ""
    if buffer:
        pieces.append(buffer)
    return pieces


def _merge_below_floor(pieces: list[str], reserve: int = 0) -> list[str]:
    """하한(120자) 미만인 조각은 다음 조각에 붙이되, 상한이 하한보다 우선이다.

    붙이면 `MAX_CHARS - reserve`(접두 붙인 뒤 실제 상한)를 넘는 경우, 짧아도 붙이지
    않고 그대로 독립 조각으로 낸다 — 하한을 지키려다 상한을 어기면 더 나쁘다.
    """
    effective_max = MAX_CHARS - reserve
    merged: list[str] = []
    buffer = ""
    for piece in pieces:
        candidate = f"{buffer} {piece}".strip() if buffer else piece
        if buffer and len(candidate) > effective_max:
            merged.append(buffer)
            buffer = piece
        else:
            buffer = candidate
        if len(buffer) >= MIN_CHARS:
            merged.append(buffer)
            buffer = ""
    if buffer:
        # "다음" 조각이 없는 채로 하한 미만이 남으면(이 구간의 마지막 조각), 유일하게
        # 붙일 수 있는 곳은 바로 앞 조각뿐이다 — 단, 그래도 상한을 넘으면 붙이지 않는다.
        candidate = f"{merged[-1]} {buffer}".strip() if merged else buffer
        if merged and len(candidate) <= effective_max:
            merged[-1] = candidate
        else:
            merged.append(buffer)
    return merged
