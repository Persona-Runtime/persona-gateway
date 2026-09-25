"""GenerationStreamResponse의 제너레이터 소유 규칙 단위 테스트(DB 없음).

규칙: 응답 처리가 끝나면(정상·연결 종료) 제너레이터를 반드시 닫는다. 다만 next()가 스레드
에서 실행 중이면 client_gone으로 깨워 그 next()가 끝난 뒤에만 close()를 부른다 — 실행 중인
제너레이터를 다른 스레드에서 닫으면 ValueError가 나기 때문이다.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator

from starlette.types import Message

from persona_minimal_api.chat.sse_stream import GenerationStreamResponse

SCOPE = {
    "type": "http",
    "asgi": {"version": "3.0", "spec_version": "2.3"},
    "http_version": "1.1",
    "method": "POST",
    "scheme": "http",
    "path": "/stream",
    "raw_path": b"/stream",
    "query_string": b"",
    "root_path": "",
    "headers": [],
    "client": ("127.0.0.1", 50000),
    "server": ("testserver", 80),
}


class Probe:
    """제너레이터 안에서 일어난 일을 순서대로 적는다."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.lock = threading.Lock()

    def add(self, event: str) -> None:
        with self.lock:
            self.events.append(event)


async def serve(response: GenerationStreamResponse, *, disconnect_after_first_body: bool) -> None:
    """ASGI로 응답을 돌린다. disconnect_after_first_body면 첫 본문 조각 뒤 연결을 끊는다."""
    first_body_sent = asyncio.Event()

    async def receive() -> Message:
        if disconnect_after_first_body:
            await first_body_sent.wait()
        else:
            await asyncio.Event().wait()  # 끊지 않는다 — 정상 완료까지 기다린다
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_body_sent.set()

    await asyncio.wait_for(response(SCOPE, receive, send), timeout=5)


def test_close_waits_for_running_next_then_closes_in_order() -> None:
    """첫 조각 뒤 next()가 client_gone을 폴링하며 막혀 있다. 연결이 끊기면 client_gone으로
    깨어난 next()가 먼저 끝나고, 그 뒤에 close()가 불린다. ValueError가 없어야 한다."""
    probe = Probe()
    client_gone = threading.Event()

    def generator() -> Iterator[str]:
        try:
            yield "event: meta\n\n"
            probe.add("waiting")
            while not client_gone.is_set():  # 업스트림 조각을 기다리는 대기 루프 흉내
                time.sleep(0.01)
            probe.add("woken")
            yield "event: late\n\n"  # 깨어난 next()가 반환하는 값
        except GeneratorExit:
            probe.add("closed")
            raise

    response = GenerationStreamResponse(generator(), client_gone, media_type="text/event-stream")
    asyncio.run(serve(response, disconnect_after_first_body=True))

    assert client_gone.is_set()
    assert probe.events == ["waiting", "woken", "closed"]


def test_generator_suspended_at_yield_receives_generator_exit_once() -> None:
    """next()가 실행 중이 아니라 yield에 멈춰 있을 때 끊기면 close()로 GeneratorExit가
    한 번 전달된다(stream_generation은 이때 reconciling을 남긴다)."""
    probe = Probe()
    client_gone = threading.Event()

    def generator() -> Iterator[str]:
        try:
            while True:
                yield "event: delta\n\n"
                time.sleep(0.01)
        except GeneratorExit:
            probe.add("closed")
            raise

    response = GenerationStreamResponse(generator(), client_gone, media_type="text/event-stream")
    asyncio.run(serve(response, disconnect_after_first_body=True))

    assert probe.events == ["closed"]


def test_exhausted_generator_is_not_closed_again() -> None:
    """정상으로 끝까지 간 제너레이터에는 정리 단계가 GeneratorExit를 만들지 않는다."""
    probe = Probe()
    client_gone = threading.Event()

    def generator() -> Iterator[str]:
        try:
            yield "event: meta\n\n"
            yield "event: done\n\n"
            probe.add("finished")
        except GeneratorExit:
            probe.add("closed")
            raise

    response = GenerationStreamResponse(generator(), client_gone, media_type="text/event-stream")
    asyncio.run(serve(response, disconnect_after_first_body=False))

    assert probe.events == ["finished"]
