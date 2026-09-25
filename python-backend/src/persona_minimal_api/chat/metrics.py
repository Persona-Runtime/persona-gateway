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
- persona_chat_time_to_first_token_seconds: 스트림 시작부터 사용자 텍스트가 담긴 첫
  delta까지. meta·citations 이벤트는 TTFT가 아니다(검색 시간은 포함된다).
- persona_chat_generation_seconds: 스트림 시작부터 terminal까지 전체 소요.
- persona_chat_stream_disconnects_total{mode}: 클라이언트 연결 종료로 generation이
  **실제로** reconciling으로 전환된 수(UPDATE가 행을 바꾼 경우만). 이미 terminal인 뒤
  닫힌 스트림은 세지 않는다. finished와 겹치지 않는다(terminal이 아니다).
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

# prometheus_client 기본 버킷은 10초가 상한이라, 첫 토큰 한도(60초)와 전체 한도(180초)
# 근처에서 무슨 일이 일어나는지 구분할 수 없었다. 두 한도를 경계로 넣어 "한도에 걸려
# 끊긴 것"과 "한도 안에서 느렸던 것"이 다른 버킷에 떨어지게 한다.
GENERATION_BUCKETS_SECONDS = (
    0.1,
    0.25,
    0.5,
    1.0,
    2.0,
    5.0,
    10.0,
    20.0,
    30.0,
    45.0,
    60.0,
    90.0,
    120.0,
    180.0,
)

TIME_TO_FIRST_TOKEN_SECONDS = Histogram(
    "persona_chat_time_to_first_token_seconds",
    "스트림 시작부터 첫 delta 이벤트까지 걸린 시간(meta·citations 제외)",
    buckets=GENERATION_BUCKETS_SECONDS,
)

TOTAL_GENERATION_SECONDS = Histogram(
    "persona_chat_generation_seconds",
    "스트림 시작부터 terminal 상태까지 걸린 전체 시간",
    buckets=GENERATION_BUCKETS_SECONDS,
)

STREAM_DISCONNECTS = Counter(
    "persona_chat_stream_disconnects_total",
    "클라이언트 연결 종료로 실제 reconciling으로 전환된 generation 수",
    ["mode"],
)
