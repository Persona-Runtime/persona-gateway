"""SSE 이벤트 직렬화. 여기 함수들은 순수 포맷팅만 한다 — DB 쓰기·upstream 호출은
`service.py`(오케스트레이션)의 책임이다.

이벤트 순서는 계약(openapi.json `x-sse-events`)이 고정한다: meta 1회 → citations
1회 → delta 0회 이상(index 단조 증가) → done 1회. header를 보낸 뒤 실패하면 done
대신 error다.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID


def _format(event: str, data: dict[str, Any]) -> str:
    # data는 JSON 한 줄이어야 한다 — SSE는 빈 줄로 이벤트 경계를 가른다. UUID는
    # json.dumps가 모르므로 str로 미리 바꾼다(default= 대신 호출자가 이미 문자열을
    # 넣게 해 이 함수는 완전히 순수하게 유지한다).
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def format_meta(
    *,
    generation_id: UUID,
    conversation_id: UUID,
    user_message_id: UUID,
    assistant_message_id: UUID,
    version_id: UUID,
    mode: str,
) -> str:
    return _format(
        "meta",
        {
            "generation_id": str(generation_id),
            "conversation_id": str(conversation_id),
            "user_message_id": str(user_message_id),
            "assistant_message_id": str(assistant_message_id),
            "version_id": str(version_id),
            "mode": mode,
        },
    )


def format_citations(*, generation_id: UUID, items: list[dict[str, Any]]) -> str:
    return _format("citations", {"generation_id": str(generation_id), "items": items})


def format_delta(*, generation_id: UUID, index: int, text: str) -> str:
    return _format("delta", {"generation_id": str(generation_id), "index": index, "text": text})


def format_done(*, generation_id: UUID, finish_reason: str) -> str:
    return _format(
        "done",
        {
            "generation_id": str(generation_id),
            "status": "completed",
            "finish_reason": finish_reason,
        },
    )


def format_error(*, generation_id: UUID, code: str, message: str, status: str) -> str:
    return _format(
        "error",
        {"generation_id": str(generation_id), "code": code, "message": message, "status": status},
    )
