"""HTTP 계측 미들웨어·build info 단위 테스트.

규칙: 요청 수·지연은 route 템플릿으로만 남고, in-flight는 정상·오류·예외·연결 종료
어느 경우에도 0으로 돌아오며, 사용자 식별자·토큰·본문은 /metrics에 나타나지 않는다.
메트릭은 프로세스 전역 REGISTRY라 테스트마다 "전후 차이"로 검증한다.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, generate_latest
from starlette.types import Message
from test_http import MemoryStore, UnexpectedFailureStore, headers, settings

from persona_minimal_api.build_info import normalized_revision, record_build_info
from persona_minimal_api.http_metrics import HttpMetricsMiddleware
from persona_minimal_api.main import create_app


def request_count(method: str, route: str, status: str, outcome: str) -> float:
    labels = {"method": method, "route": route, "status": status, "outcome": outcome}
    return REGISTRY.get_sample_value("persona_http_requests_total", labels) or 0.0


def duration_count(method: str, route: str, status_class: str, outcome: str) -> float:
    labels = {"method": method, "route": route, "status_class": status_class, "outcome": outcome}
    return REGISTRY.get_sample_value("persona_http_request_duration_seconds_count", labels) or 0.0


def in_flight() -> float:
    return REGISTRY.get_sample_value("persona_http_requests_in_flight") or 0.0


def isolated_app() -> FastAPI:
    """앱 전체 없이 미들웨어 규칙만 보는 작은 앱. 경로 이름은 실제 앱과 겹치지 않게 한다."""
    app = FastAPI()

    @app.get("/metrics-test/items/{item_id}")
    def read_item(item_id: str) -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/metrics-test/boom")
    def boom() -> dict[str, str]:
        raise RuntimeError("synthetic failure with secret-question-text")

    app.add_middleware(HttpMetricsMiddleware)
    return app


def test_success_is_counted_by_route_template_not_actual_path() -> None:
    # 준비: 실제 경로에 들어갈 합성 ID
    item_id = str(uuid4())
    route = "/metrics-test/items/{item_id}"
    before = request_count("GET", route, "200", "completed")
    before_duration = duration_count("GET", route, "2xx", "completed")

    # 실행
    response = TestClient(isolated_app()).get(f"/metrics-test/items/{item_id}")

    # 검증: 템플릿 label로 1건, 지연 1건, in-flight 복귀. 실제 ID는 어디에도 없다.
    assert response.status_code == 200
    assert request_count("GET", route, "200", "completed") == before + 1
    assert duration_count("GET", route, "2xx", "completed") == before_duration + 1
    assert in_flight() == 0
    assert item_id not in generate_latest().decode()


def test_unmatched_path_uses_fixed_label() -> None:
    before = request_count("GET", "unmatched", "404", "completed")

    response = TestClient(isolated_app()).get(f"/no-such-path/{uuid4()}")

    assert response.status_code == 404
    assert request_count("GET", "unmatched", "404", "completed") == before + 1
    assert in_flight() == 0


def test_route_exception_is_counted_as_500_exception_and_in_flight_returns_to_zero() -> None:
    route = "/metrics-test/boom"
    before = request_count("GET", route, "500", "exception")
    before_duration = duration_count("GET", route, "5xx", "exception")

    response = TestClient(isolated_app(), raise_server_exceptions=False).get(route)

    assert response.status_code == 500
    assert request_count("GET", route, "500", "exception") == before + 1
    assert duration_count("GET", route, "5xx", "exception") == before_duration + 1
    assert in_flight() == 0


STREAM_ROUTE = "/metrics-test/stream"


def stream_app(*, with_header_middleware: bool = False) -> FastAPI:
    """같은 route에서 끝까지 가는 스트림(`?finite=1`)과 끝나지 않는 스트림을 모두 낸다.
    쿼리 문자열은 label에 들어가지 않으므로 두 요청은 같은 route label을 쓴다.

    with_header_middleware: main.py의 헤더 미들웨어처럼 `@app.middleware("http")`
    (BaseHTTPMiddleware)를 안쪽에 끼운다. 이 미들웨어는 disconnect를 받은 뒤에도 마지막
    본문을 흘려보내므로 판정이 뒤집히지 않는지 따로 확인해야 한다.
    """
    app = FastAPI()

    async def finite_stream() -> AsyncIterator[str]:
        yield "event: meta\ndata: {}\n\n"
        yield "event: done\ndata: {}\n\n"

    async def endless_stream() -> AsyncIterator[str]:
        yield "event: meta\ndata: {}\n\n"
        while True:
            await asyncio.sleep(0.01)
            yield ": keepalive\n\n"

    @app.get(STREAM_ROUTE)
    def stream(finite: bool = False) -> StreamingResponse:
        body = finite_stream() if finite else endless_stream()
        return StreamingResponse(body, media_type="text/event-stream")

    if with_header_middleware:

        @app.middleware("http")
        async def add_header(request: Request, call_next):  # type: ignore[no-untyped-def]
            response = await call_next(request)
            response.headers["X-Test"] = "1"
            return response

    app.add_middleware(HttpMetricsMiddleware)
    return app


async def request_then_disconnect(app: FastAPI, path: str) -> list[Message]:
    """ASGI를 직접 불러 첫 본문 조각을 받은 직후 http.disconnect를 흘린다.
    TestClient는 응답을 끝까지 읽으므로 '도중 연결 종료'를 만들 수 없다."""
    first_body_sent = asyncio.Event()
    sent: list[Message] = []

    async def receive() -> Message:
        await first_body_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)
        if message["type"] == "http.response.body":
            first_body_sent.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    return sent


def test_stream_closed_by_client_is_counted_as_disconnect_and_in_flight_returns_to_zero() -> None:
    """SSE 도중 클라이언트가 끊는 경우. StreamingResponse는 disconnect를 받으면 예외 없이
    끝나므로, 미들웨어가 receive에서 본 disconnect로 판정해야 한다."""
    before = request_count("GET", STREAM_ROUTE, "200", "client_disconnected")
    before_completed = request_count("GET", STREAM_ROUTE, "200", "completed")

    sent = asyncio.run(request_then_disconnect(stream_app(), STREAM_ROUTE))

    # 전제 확인: 마지막 본문(more_body=False)은 나가지 않았다.
    assert not any(
        m["type"] == "http.response.body" and not m.get("more_body", False) for m in sent
    )
    assert request_count("GET", STREAM_ROUTE, "200", "client_disconnected") == before + 1
    assert request_count("GET", STREAM_ROUTE, "200", "completed") == before_completed
    assert in_flight() == 0


def test_disconnect_through_header_middleware_is_not_turned_into_completed() -> None:
    """BaseHTTPMiddleware는 disconnect 뒤에도 more_body=False를 보낸다. 완료 전에 받은
    disconnect가 있으면 그 뒤의 완료 표시와 무관하게 client_disconnected여야 한다."""
    before = request_count("GET", STREAM_ROUTE, "200", "client_disconnected")
    before_completed = request_count("GET", STREAM_ROUTE, "200", "completed")

    asyncio.run(request_then_disconnect(stream_app(with_header_middleware=True), STREAM_ROUTE))

    assert request_count("GET", STREAM_ROUTE, "200", "client_disconnected") == before + 1
    assert request_count("GET", STREAM_ROUTE, "200", "completed") == before_completed
    assert in_flight() == 0


def test_completed_and_disconnected_requests_land_in_separate_duration_series() -> None:
    """같은 route라도 끝까지 간 요청과 끊긴 요청의 지연이 다른 histogram series에 들어가야
    정상 API p99(outcome="completed")를 따로 읽을 수 있다."""
    app = stream_app()
    completed_before = duration_count("GET", STREAM_ROUTE, "2xx", "completed")
    disconnected_before = duration_count("GET", STREAM_ROUTE, "2xx", "client_disconnected")

    response = TestClient(app).get(f"{STREAM_ROUTE}?finite=1")
    asyncio.run(request_then_disconnect(app, STREAM_ROUTE))

    assert response.status_code == 200
    assert duration_count("GET", STREAM_ROUTE, "2xx", "completed") == completed_before + 1
    assert (
        duration_count("GET", STREAM_ROUTE, "2xx", "client_disconnected") == disconnected_before + 1
    )
    assert in_flight() == 0


def test_create_app_wires_metrics_for_auth_errors_and_failures() -> None:
    api = TestClient(create_app(settings(), MemoryStore()))
    before_ok = request_count("GET", "/v1/me", "200", "completed")
    before_unauthorized = request_count("GET", "/v1/me", "401", "completed")

    assert api.get("/v1/me", headers=headers()).status_code == 200
    assert api.get("/v1/me").status_code == 401

    assert request_count("GET", "/v1/me", "200", "completed") == before_ok + 1
    assert request_count("GET", "/v1/me", "401", "completed") == before_unauthorized + 1
    assert in_flight() == 0

    failing = TestClient(
        create_app(settings(), UnexpectedFailureStore()), raise_server_exceptions=False
    )
    before_failure = request_count("GET", "/v1/personas", "500", "exception")
    assert failing.get("/v1/personas", headers=headers()).status_code == 500
    assert request_count("GET", "/v1/personas", "500", "exception") == before_failure + 1
    assert in_flight() == 0


def test_metrics_endpoint_is_not_counted_and_contains_no_identifiers() -> None:
    api = TestClient(create_app(settings(), MemoryStore()))
    idempotency_key = uuid4()
    api.post("/v1/personas", headers=headers(idempotency_key), json={"name": "metrics_probe_name"})

    before = request_count("GET", "/metrics", "200", "completed")
    body = api.get("/metrics").text

    assert request_count("GET", "/metrics", "200", "completed") == before
    for secret in (
        "synthetic-token",
        "synthetic-owner",
        "metrics_probe_name",
        str(idempotency_key),
    ):
        assert secret not in body
    # route label에는 템플릿만 있다 — 중괄호 없는 UUID 같은 실제 값이 오면 안 된다.
    for line in body.splitlines():
        if line.startswith("persona_http_requests_total{"):
            route = line.split('route="', 1)[1].split('"', 1)[0]
            assert route == "unmatched" or route.startswith("/")
    assert "persona_gateway_build_info{" in body
    # outcome label은 세 값만 쓴다 — counter와 histogram 모두.
    allowed_outcomes = {"completed", "client_disconnected", "exception"}
    for line in body.splitlines():
        if line.startswith(
            ("persona_http_requests_total{", "persona_http_request_duration_seconds_count{")
        ):
            outcome = line.split('outcome="', 1)[1].split('"', 1)[0]
            assert outcome in allowed_outcomes


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0123abcd", "0123abcd"),
        ("ABCDEF0123456789ABCDEF0123456789ABCDEF01", "abcdef0123456789abcdef0123456789abcdef01"),
        (None, "unknown"),
        ("", "unknown"),
        ("main", "unknown"),
        ("0123abc; rm -rf", "unknown"),
        ("012345", "unknown"),
    ],
)
def test_revision_label_accepts_only_git_hash(raw: str | None, expected: str) -> None:
    assert normalized_revision(raw) == expected


def test_build_info_does_not_grow_series_when_recorded_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSONA_BUILD_REVISION", "0123abcd")
    record_build_info()
    record_build_info()

    samples = [
        sample
        for metric in REGISTRY.collect()
        if metric.name == "persona_gateway_build"
        for sample in metric.samples
    ]
    assert len(samples) == 1
    assert samples[0].labels["revision"] == "0123abcd"
