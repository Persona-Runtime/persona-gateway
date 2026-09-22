"""채팅 generation 저카디널리티 메트릭.

retrieval/metrics.py(RETRIEVAL_SECONDS)와 같은 패턴 — 새 의존성 없이 이미 쓰는
prometheus_client만 쓴다. `/metrics`는 main.py가 이미 노출하므로 별도 배선이 없다.

라벨은 status·mode·terminal_reason처럼 값의 가짓수가 고정·적은 것만 쓴다.
user_id·persona_id·conversation_id·generation_id·질문/답변 본문은 라벨로도 값으로도
넣지 않는다 — 라벨 카디널리티 폭발과 별개로, 그 자체가 사용자 입력을 관측 계로
새어 나가게 하는 것이기 때문이다(AGENTS.md 공개 검증 규칙과 같은 이유).

ServiceMonitor/PodMonitor·대시보드는 persona-platform 쪽 별도 작업이다(이 파일의
범위 밖) — 거기서 참고할 이름·의미만 여기 모아 둔다.

- persona_chat_generations_started_total{mode}: 접수(6단계 진입) 수.
- persona_chat_generations_finished_total{mode,terminal_reason}: terminal 도달 수.
  terminal_reason은 generations.status의 terminal 값(completed/cancelled/failed)과
  같다.
- persona_chat_time_to_first_token_seconds: 접수부터 첫 delta까지.
- persona_chat_generation_seconds: 접수부터 terminal까지 전체 소요.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

GENERATIONS_STARTED = Counter(
    "persona_chat_generations_started_total",
    "채팅 generation 접수(스트림 시작) 수",
    ["mode"],
)

GENERATIONS_FINISHED = Counter(
    "persona_chat_generations_finished_total",
    "채팅 generation이 terminal 상태에 도달한 수",
    ["mode", "terminal_reason"],
)

TIME_TO_FIRST_TOKEN_SECONDS = Histogram(
    "persona_chat_time_to_first_token_seconds",
    "접수부터 첫 delta 이벤트까지 걸린 시간",
)

TOTAL_GENERATION_SECONDS = Histogram(
    "persona_chat_generation_seconds",
    "접수부터 terminal 상태까지 걸린 전체 시간",
)
