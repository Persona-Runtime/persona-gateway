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
        별도 스레드/요청에서 부를 수 있다는 전제로 동작해야 한다.

        **예외 없이 이터레이터가 끝나는 것은 오직 취소일 때만 허용된다.** 그 외의
        비정상 조기 종료(업스트림 연결이 중간에 끊김·타임아웃·형식 오류 등)는 반드시
        예외(예: `UpstreamError`)를 던져야 한다 — 호출자는 "예외 없이 멈춤"과 "중간에
        끊김"을 이 규칙 하나로만 구분하므로, 조용히 멈추면서 그게 실은 장애였던
        경우를 만들면 정상 완료로 잘못 기록된다."""
        ...

    def cancel(self, generation_id: UUID) -> None:
        """이미 끝났거나 모르는 generation_id를 넘겨도 조용히 무시한다(멱등)."""
        ...
