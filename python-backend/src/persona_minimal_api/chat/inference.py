"""업스트림(생성 모델) 어댑터가 지켜야 할 최소 인터페이스.

Gateway는 "지금 upstream이 Fake인지 실제 vLLM인지"를 설정값으로만 안다 — RAG·권한·
저장·SSE 규칙은 두 구현이 동일하게 거친다. 이번 라운드는 `fake_inference.py`만
구현한다. 실제 vLLM adapter(`vllm_client.py`)는 GPU 연결 시점에 별도로 만든다 —
검증할 실제 vLLM이 없는 채로 작성해 두면 아무도 실행해 보지 않은 코드가 된다.

취소는 어댑터 책임이다: 실제 vLLM이었다면 업스트림에도 취소를 통지해야 하므로,
"신호만 세우고 Gateway가 알아서 멈추는" 방식이 아니라 어댑터가 `cancel()`을 받아
직접 처리하게 한다.
"""

from __future__ import annotations

from typing import Iterator, Protocol
from uuid import UUID

from ..retrieval.prompt import Message


class InferenceClient(Protocol):
    def start(
        self, generation_id: UUID, messages: list[Message], *, max_tokens: int
    ) -> Iterator[str]:
        """토큰(문자열 조각) 스트림을 돈다. 취소되면 남은 조각을 만들지 않고 멈춘다 —
        예외를 던지지 않는다(취소는 실패가 아니다). 호출자가 `cancel(generation_id)`를
        별도 스레드/요청에서 부를 수 있다는 전제로 동작해야 한다."""
        ...

    def cancel(self, generation_id: UUID) -> None:
        """이미 끝났거나 모르는 generation_id를 넘겨도 조용히 무시한다(멱등)."""
        ...
