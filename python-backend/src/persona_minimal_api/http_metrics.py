"""HTTP 요청 수·지연·진행 중 요청을 재는 순수 ASGI 미들웨어.

무중단 롤아웃과 DB 장애 전환 중 사용자가 받은 영향(성공/실패, 지연 꼬리, 끊긴 요청)을
Prometheus로 읽기 위한 것이다. `/metrics`(main.py)가 기본 REGISTRY로 이 값들을 노출한다.

`BaseHTTPMiddleware`(`@app.middleware("http")`)를 쓰지 않는 이유: 그 방식은 `call_next`가
응답 헤더를 돌려주는 시점에 끝나서, SSE처럼 본문이 길게 이어지는 응답의 실제 소요 시간과
도중 연결 종료를 볼 수 없다. 여기서는 `send`를 감싸 마지막 본문 조각이 나갈 때까지 잰다.

label 규칙 — 값의 가짓수가 작고 사용자 정보가 없는 것만 쓴다.
- route: 실제 경로가 아니라 라우트 템플릿(`/v1/generations/{generation_id}/cancel`).
  매칭된 라우트가 없으면 `unmatched`. 경로 안의 ID·쿼리 문자열은 어디에도 넣지 않는다.
- method: HTTP method. 앱이 쓰지 않는 method는 `other`로 묶는다.
- status: 응답 코드. 응답을 시작하기 전에 예외가 나면 `500`(ServerErrorMiddleware가
  실제로 보내는 값과 같다), 응답 전에 클라이언트가 끊으면 `499`.
- outcome: `completed` / `client_disconnected` / `exception` 세 값뿐이다. counter와
  histogram 모두에 붙는다. 일반 API 지연(p50/p95/p99)은 반드시 `outcome="completed"`로
  조회한다 — 끊긴 요청은 끊긴 시점까지의 시간이라 정상 지연 분포에 섞이면 안 된다.
사용자 ID·대화 ID·Idempotency-Key·본문·토큰·예외 메시지는 label로도 값으로도 쓰지 않는다.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from prometheus_client import Counter, Gauge, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# 짧은 API(수 ms~1초)와 채팅 SSE(첫 토큰 한도 60초, 전체 한도 180초)를 한 histogram에서
# 구분하려는 경계다. 60·180은 계약 한도라 그대로 경계로 두고, 300은 reconciling을 닫는
# 기준 시간이다. 1초 이하를 촘촘히 둔 이유는 롤아웃·DB 전환 중 일반 API 지연이 몇 배
# 늘어나는지를 보기 위해서다.
REQUEST_DURATION_BUCKETS_SECONDS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    90.0,
    120.0,
    180.0,
    300.0,
)

OUTCOME_COMPLETED = "completed"
OUTCOME_CLIENT_DISCONNECTED = "client_disconnected"
OUTCOME_EXCEPTION = "exception"
UNMATCHED_ROUTE = "unmatched"

# Prometheus scrape 요청은 세지 않는다 — 30초마다 들어오는 자기 참조 요청이 요청 수와
# 지연 분포를 흐리게 만든다. scrape 성공 여부는 Prometheus의 `up`으로 따로 본다.
EXCLUDED_PATHS = frozenset({"/metrics"})

KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})

HTTP_REQUESTS = Counter(
    "persona_http_requests_total",
    "HTTP 요청 수(라우트 템플릿·상태 코드·결과별)",
    ["method", "route", "status", "outcome"],
)

# outcome을 label로 둔 이유: 롤아웃·DB 전환 중에는 끊긴 요청이 몰리는데, 그 시간이 정상
# 완료 요청과 같은 series에 섞이면 정상 API p99를 따로 읽을 수 없다.
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "persona_http_request_duration_seconds",
    "요청 수신부터 마지막 응답 본문 전송(또는 연결 종료·예외)까지 걸린 시간",
    ["method", "route", "status_class", "outcome"],
    buckets=REQUEST_DURATION_BUCKETS_SECONDS,
)

# route label이 없는 이유: 미들웨어에 들어오는 시점에는 라우팅 전이라 route를 모른다.
# 들어올 때 올리고 나갈 때 내리는 짝을 정확히 맞추는 쪽을 택했다.
HTTP_REQUESTS_IN_FLIGHT = Gauge(
    "persona_http_requests_in_flight",
    "지금 처리 중인 HTTP 요청 수(SSE 스트림 포함)",
)


def route_template(scope: Scope) -> str:
    """라우팅이 끝난 scope에서 라우트 템플릿을 꺼낸다. 없으면 `unmatched`."""
    route: Any = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    return template if isinstance(template, str) and template else UNMATCHED_ROUTE


def status_class(status: int) -> str:
    return f"{status // 100}xx"


class HttpMetricsMiddleware:
    """HTTP 요청 하나마다 수·지연·in-flight를 기록한다. 응답 내용은 바꾸지 않는다."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in EXCLUDED_PATHS:
            await self.app(scope, receive, send)
            return

        started_at = time.perf_counter()
        status_code: int | None = None
        response_complete = False
        client_disconnected = False
        outcome = OUTCOME_COMPLETED

        async def receive_watching_disconnect() -> Message:
            nonlocal client_disconnected
            message = await receive()
            # 응답을 다 보낸 뒤에 받는 http.disconnect는 정상 종료 뒤의 연결 정리일 뿐이라
            # 세지 않는다(uvicorn은 응답 완료 뒤 receive에 disconnect를 돌려준다).
            if message["type"] == "http.disconnect" and not response_complete:
                client_disconnected = True
            return message

        async def send_recording_status(message: Message) -> None:
            nonlocal status_code, response_complete
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                response_complete = True
            await send(message)

        # 올린 in-flight는 어떤 경로로 끝나든 finally에서 반드시 내린다 — 예외·취소·연결
        # 종료에서 내리지 않으면 값이 계속 쌓여 "처리 중 요청"이 거짓이 된다.
        HTTP_REQUESTS_IN_FLIGHT.inc()
        try:
            await self.app(scope, receive_watching_disconnect, send_recording_status)
        except BaseException as error:
            # 기록만 하고 예외는 그대로 다시 던진다 — 측정 때문에 실패를 성공으로 바꾸지
            # 않는다. 취소(CancelledError — uvicorn은 asyncio로 돈다)와 전송 실패(OSError)는
            # 클라이언트가 먼저 끊은 경우로 본다. 예외 메시지는 사용자 입력을 담을 수 있어
            # 어디에도 남기지 않는다.
            if client_disconnected or isinstance(error, (OSError, asyncio.CancelledError)):
                outcome = OUTCOME_CLIENT_DISCONNECTED
            else:
                outcome = OUTCOME_EXCEPTION
            raise
        finally:
            HTTP_REQUESTS_IN_FLIGHT.dec()
            if outcome == OUTCOME_COMPLETED and client_disconnected:
                # StreamingResponse는 연결 종료를 감지하면 예외 없이 조용히 끝난다. 게다가
                # BaseHTTPMiddleware(main.py의 헤더 미들웨어)는 disconnect를 받은 **뒤에도**
                # 마지막 본문(more_body=False)을 흘려보낸다 — 실제 uvicorn에서 관찰함.
                # 그래서 "완료 전에 disconnect를 받았는가"만으로 판정하고, 그 뒤의 완료
                # 표시는 보지 않는다(클라이언트는 이미 떠나 그 조각을 받지 못했다).
                outcome = OUTCOME_CLIENT_DISCONNECTED
            self._record(scope, status_code, outcome, time.perf_counter() - started_at)

    @staticmethod
    def _record(scope: Scope, status_code: int | None, outcome: str, seconds: float) -> None:
        method = scope.get("method", "")
        method_label = method if method in KNOWN_METHODS else "other"
        route = route_template(scope)
        # 응답을 시작하기 전에 끝났다면 클라이언트가 받은 코드가 없다. 예외면 서버가 500을
        # 보내므로 500, 그 전에 연결이 끊겼다면 nginx 관례를 따라 499로 구분한다.
        if status_code is None:
            status_code = 499 if outcome == OUTCOME_CLIENT_DISCONNECTED else 500
        HTTP_REQUESTS.labels(method_label, route, str(status_code), outcome).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(
            method_label, route, status_class(status_code), outcome
        ).observe(seconds)
