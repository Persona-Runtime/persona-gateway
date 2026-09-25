"""개발/테스트 전용 `InferenceClient` 구현. 실제 모델을 호출하지 않는다.

기본 인스턴스(인자 없이 생성)는 정상 chunk 3~5개 뒤 종료하는 조용한 동작이다.
9개 실패 시나리오(느린 첫 토큰, 전체 초과, 중단, 잘린 UTF-8, 취소 후 늦은 토큰,
업스트림 오류)는 **유저 입력으로 트리거하지 않는다** — 매직 스트링을 프로덕션
코드에 심지 않는다. 대신 테스트가 생성자 인자로 원하는 시나리오를 직접 구성한
`FakeInferenceClient` 인스턴스를 만들어 `create_app(inference_client=...)`에
주입한다. 60초/180초 시간 제한, "동일 Idempotency-Key 재전송", "SSE 이벤트가 여러
network read로 나뉨"은 이 어댑터가 아니라 서비스/SSE 계층의 책임이라 거기서
검증한다 — 여기서는 청크와 그 사이의 인위적 지연만 만든다.
"""

from __future__ import annotations

import threading
import time
from typing import Iterator
from uuid import UUID

from ..retrieval.prompt import Message

# UpstreamError는 vLLM adapter와 함께 쓰려고 inference.py로 옮겼다. 기존 테스트가
# 여기서 import하므로 같은 이름으로 다시 내보낸다.
from .inference import UpstreamError

__all__ = ["DEFAULT_CHUNKS", "FakeInferenceClient", "UpstreamError"]

DEFAULT_CHUNKS = ("합성 ", "응답", "입니다.")


class FakeInferenceClient:
    def __init__(
        self,
        *,
        chunks: tuple[str, ...] = DEFAULT_CHUNKS,
        delay_before_first_chunk: float = 0.0,
        delay_between_chunks: float = 0.0,
        raise_before_start: Exception | None = None,
        stop_after: int | None = None,
    ):
        """
        chunks: 순서대로 내보낼 조각들. 잘린 UTF-8·이벤트 분할 시나리오는 호출자가
            원하는 모양으로 조각을 직접 나눠 넣으면 된다(예: 멀티바이트 문자 중간에서
            끊긴 조각).
        delay_before_first_chunk / delay_between_chunks: 초 단위 인위적 지연 — "느린
            첫 토큰"·"전체 초과" 시나리오는 서비스 계층의 60초/180초 판정 로직을
            테스트할 때 이 값을 작게(예: 0.05초) 주고 서비스 쪽 deadline 상수를 함께
            줄여서 검증한다(실제로 60~180초를 기다리지 않는다).
        raise_before_start: 설정하면 `start()` 호출 즉시 이 예외를 던진다(업스트림
            429/503/형식 오류 시뮬레이션 — `UpstreamError` 사용을 권장).
        stop_after: 설정하면 그 개수만큼만 내보내고 `UpstreamError("upstream_
            disconnected")`를 던진다(정상 done이 아니라 "중간 upstream 단절"
            시뮬레이션). Protocol 계약상 예외 없이 조용히 멈추는 건 취소 전용이라
            — 단순 `return`으로는 정상 완료와 구분되지 않는다.
        """
        self._chunks = chunks
        self._delay_before_first_chunk = delay_before_first_chunk
        self._delay_between_chunks = delay_between_chunks
        self._raise_before_start = raise_before_start
        self._stop_after = stop_after
        self._cancelled: dict[UUID, threading.Event] = {}
        self._lock = threading.Lock()

    def _cancel_event(self, generation_id: UUID) -> threading.Event:
        with self._lock:
            event = self._cancelled.get(generation_id)
            if event is None:
                event = threading.Event()
                self._cancelled[generation_id] = event
            return event

    def start(
        self, generation_id: UUID, messages: list[Message], *, max_tokens: int
    ) -> Iterator[str]:
        del messages, max_tokens  # Fake는 실제 프롬프트 내용에 반응하지 않는다.
        if self._raise_before_start is not None:
            raise self._raise_before_start

        event = self._cancel_event(generation_id)
        time.sleep(self._delay_before_first_chunk)
        emitted = 0
        for chunk in self._chunks:
            if self._stop_after is not None and emitted >= self._stop_after:
                # 중간 upstream 단절 — 취소가 아니므로 조용히 return하지 않는다
                # (Protocol 계약: 예외 없는 종료는 취소 전용).
                raise UpstreamError("upstream_disconnected")
            if event.is_set():
                # 취소됨 — 남은 조각을 더 만들지 않고 그냥 반환한다(예외 아님).
                return
            yield chunk
            emitted += 1
            time.sleep(self._delay_between_chunks)

    def cancel(self, generation_id: UUID) -> None:
        self._cancel_event(generation_id).set()
