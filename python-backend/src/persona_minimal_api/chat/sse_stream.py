"""채팅 SSE 제너레이터를 소유하고, 연결이 끝나면 반드시 닫는 StreamingResponse.

왜 필요한가: Starlette `StreamingResponse`에 sync 제너레이터를 그대로 넘기면, 클라이언트가
연결을 끊었을 때 반복만 멈추고 제너레이터를 닫지 않는다. 제너레이터는 가비지 컬렉션이 돌 때
에야 `GeneratorExit`를 받으므로, generation이 그때까지 running에 남고 reconciling 전환과
disconnect 지표가 GC 시점에 좌우됐다(2026-09-25 실제 uvicorn에서 관찰).

이 응답은 두 가지를 보장한다.
1. 응답 처리가 어떤 식으로 끝나든(정상 완료, 연결 종료에 따른 취소) `__call__`의 finally가
   정리 단계를 실행한다. GC와 무관하다.
2. **제너레이터를 두 스레드가 동시에 만지지 않는다.** `next()`는 스레드풀에서 한 번에 하나씩
   실행되며, 그 `next()`가 실행 중일 때 다른 스레드에서 `close()`를 부르면
   `ValueError: generator already executing`이 난다. 그래서 정리는 다음 순서를 따른다.
   (a) `client_gone`을 세운다 — 업스트림 조각을 기다리던 `next()`가
       `CLIENT_GONE_POLL_SECONDS` 안에 깨어나 reconciling을 남기고 끝난다.
   (b) 진행 중이던 `next()`가 끝날 때까지 기다린다.
   (c) 그다음에만 `close()`를 부른다 — yield에 멈춰 있던 제너레이터는 `GeneratorExit`로
       reconciling을 남기고, 이미 끝난 제너레이터에는 아무 일도 없다.

한계: 즉시 전환을 보장하지 않는다. 검색·embedding·DB 조회 같은 동기 선행 호출은
`client_gone`을 확인하지 않으므로, 그 안에 `next()`가 있으면 (b)는 해당 호출이 반환하거나
timeout될 때까지 기다리고 전환도 그때 일어난다(통합 테스트
test_real_uvicorn_close_during_retrieval_waits_for_call_to_return이 이 경계를 고정한다).
업스트림(vLLM) 요청은 닫지 않는다 — 연결 종료는 취소가 아니다(README 채팅 업스트림).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Generator
from typing import Any

import anyio
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

# next()가 StopIteration으로 끝났음을 스레드 밖으로 전하는 표시. StopIteration은
# 코루틴·future 경계를 넘으면 RuntimeError로 바뀌므로 예외로 넘기지 않는다.
_EXHAUSTED = object()


def _next_or_exhausted(generator: Generator[str, None, None]) -> Any:
    try:
        return next(generator)
    except StopIteration:
        return _EXHAUSTED


class GenerationStreamResponse(StreamingResponse):
    """`stream_generation` 제너레이터와 그 `client_gone` 신호를 함께 받아 소유한다.

    client_gone은 같은 객체를 `stream_generation(client_gone=...)`에도 넘겨야 한다 — 이
    응답이 세우고, 제너레이터의 대기 루프가 확인한다.
    """

    def __init__(
        self,
        generator: Generator[str, None, None],
        client_gone: threading.Event,
        **kwargs: Any,
    ) -> None:
        self._generator = generator
        self._client_gone = client_gone
        # 지금 스레드풀에서 실행 중인 next(). 정리 단계가 이것이 끝나기를 기다린다.
        self._pending_next: asyncio.Future[Any] | None = None
        super().__init__(self._iterate(), **kwargs)

    async def _iterate(self) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        while True:
            self._pending_next = loop.run_in_executor(None, _next_or_exhausted, self._generator)
            # shield: 연결 종료로 이 await가 취소돼도 스레드의 next()를 추적하는 future는
            # 취소되지 않고 남는다. 정리 단계가 그 future로 next() 종료를 기다린다.
            item = await asyncio.shield(self._pending_next)
            self._pending_next = None
            if item is _EXHAUSTED:
                return
            yield item

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # anyio 취소는 취소된 범위 안의 await마다 다시 전달된다. 정리 도중 다시 취소되면
            # 제너레이터가 닫히지 않은 채 남으므로 이 구간만 취소에서 보호한다.
            with anyio.CancelScope(shield=True):
                await self._close_owned_generator()

    async def _close_owned_generator(self) -> None:
        # (a) 업스트림 조각을 기다리던 next()를 깨운다. 정상 완료 뒤에는 아무 효과가 없다.
        self._client_gone.set()
        # (b) 실행 중인 next()가 끝나야 close()를 부를 수 있다.
        # (c) 그다음 같은 소유자가 닫는다. 이미 끝났으면 no-op이다. next()가 예외로 끝났어도
        #     close()는 반드시 부르고, 예외는 삼키지 않고 그대로 올린다(stream_generation의
        #     안전망이 예상한 오류는 이미 처리하므로, 여기까지 오는 것은 진짜 결함이다).
        pending = self._pending_next
        try:
            if pending is not None:
                await pending
        finally:
            await anyio.to_thread.run_sync(self._generator.close)
