from __future__ import annotations

import json
from uuid import uuid4

from persona_minimal_api.chat.fake_inference import FakeInferenceClient, UpstreamError
from persona_minimal_api.chat.sse import (
    format_citations,
    format_delta,
    format_done,
    format_error,
    format_meta,
)


def _parse(event: str) -> tuple[str, dict]:
    lines = event.strip("\n").split("\n")
    assert lines[0].startswith("event: ")
    assert lines[1].startswith("data: ")
    return lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))


def test_meta_citations_delta_done_format_and_end_with_blank_line() -> None:
    gid, cid, umid, amid, vid = (uuid4() for _ in range(5))
    meta = format_meta(
        generation_id=gid,
        conversation_id=cid,
        user_message_id=umid,
        assistant_message_id=amid,
        version_id=vid,
        mode="mock",
    )
    assert meta.endswith("\n\n")
    name, data = _parse(meta)
    assert name == "meta"
    assert data == {
        "generation_id": str(gid),
        "conversation_id": str(cid),
        "user_message_id": str(umid),
        "assistant_message_id": str(amid),
        "version_id": str(vid),
        "mode": "mock",
    }

    name, data = _parse(format_citations(generation_id=gid, items=[{"id": "x"}]))
    assert name == "citations"
    assert data == {"generation_id": str(gid), "items": [{"id": "x"}]}

    name, data = _parse(format_delta(generation_id=gid, index=0, text="합성"))
    assert name == "delta"
    assert data == {"generation_id": str(gid), "index": 0, "text": "합성"}

    name, data = _parse(format_done(generation_id=gid, finish_reason="stop"))
    assert name == "done"
    assert data == {"generation_id": str(gid), "status": "completed", "finish_reason": "stop"}


def test_error_event_carries_status() -> None:
    gid = uuid4()
    name, data = _parse(
        format_error(generation_id=gid, code="backend_unavailable", message="msg", status="failed")
    )
    assert name == "error"
    assert data["status"] == "failed"
    assert data["code"] == "backend_unavailable"


def test_fake_inference_default_yields_three_chunks() -> None:
    client = FakeInferenceClient()
    chunks = list(client.start(uuid4(), messages=[], max_tokens=512))
    assert chunks == ["합성 ", "응답", "입니다."]


def test_fake_inference_cancel_stops_remaining_chunks() -> None:
    client = FakeInferenceClient(chunks=("a", "b", "c", "d"))
    generation_id = uuid4()
    received = []
    for chunk in client.start(generation_id, messages=[], max_tokens=512):
        received.append(chunk)
        if chunk == "b":
            client.cancel(generation_id)
    # 취소가 "b" 처리 중 걸렸으므로 그 뒤(c, d)는 나오지 않는다.
    assert received == ["a", "b"]


def test_fake_inference_stop_after_simulates_mid_stream_disconnect() -> None:
    client = FakeInferenceClient(chunks=("a", "b", "c"), stop_after=1)
    chunks = list(client.start(uuid4(), messages=[], max_tokens=512))
    assert chunks == ["a"]


def test_fake_inference_raise_before_start_simulates_upstream_error() -> None:
    client = FakeInferenceClient(raise_before_start=UpstreamError("upstream_503"))
    generation_id = uuid4()
    try:
        list(client.start(generation_id, messages=[], max_tokens=512))
        raised = False
    except UpstreamError as error:
        raised = True
        assert error.code == "upstream_503"
    assert raised


def test_fake_inference_truncated_utf8_chunk_is_passed_through_as_is() -> None:
    # 잘린 UTF-8 문자 시나리오 — Fake는 의도적으로 멀티바이트 문자 중간에서 끊긴
    # 조각을 그대로 낸다. 서비스/SSE 계층이 이를 텍스트로 그대로 전달하는지(깨진
    # 조각을 삼키거나 예외를 내지 않는지)는 이 테스트가 아니라 서비스 계층 테스트의
    # 책임이다 — 여기서는 Fake가 요청한 조각을 그대로 재현하는지만 본다.
    broken = "한".encode("utf-8")[:2].decode("utf-8", errors="surrogateescape")
    client = FakeInferenceClient(chunks=(broken, "나머지"))
    chunks = list(client.start(uuid4(), messages=[], max_tokens=512))
    assert chunks[0] == broken
