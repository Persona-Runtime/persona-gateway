"""vllm_client.py 단위 테스트 — 실제 GPU·vLLM 없이 합성 OpenAI 호환 SSE 서버로.

`httpx.MockTransport`(test_embedding_client.py의 방식)는 응답을 통째로 버퍼에 담아
돌려주므로 "토큰 사이 무응답", "[DONE] 없이 연결 끊김", "취소 때 실제로 연결이 닫히는가"를
증명하지 못한다. 그래서 여기서는 표준 라이브러리 HTTP 서버를 port 0으로 띄워 진짜 소켓을
쓴다(새 의존성 없음).

여기서 통과하는 것은 **어댑터가 합성 서버와 맞물린다**는 증거이지, 실제 vLLM 동작의
증거가 아니다.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from persona_minimal_api.chat.fake_inference import DEFAULT_CHUNKS, FakeInferenceClient
from persona_minimal_api.chat.inference import UpstreamError
from persona_minimal_api.chat.vllm_client import VllmInferenceClient
from persona_minimal_api.config import Settings
from persona_minimal_api.main import build_inference_client
from persona_minimal_api.retrieval.prompt import Message

# 합성 값만 쓴다. 실제 프롬프트·토큰·사용자 자료를 테스트에 넣지 않는다.
SYNTHETIC_QUESTION = "합성 질문 — 오류 메시지에 남으면 안 된다"
SYNTHETIC_API_KEY = "synthetic-vllm-key-not-real"
MESSAGES = [
    Message(role="system", content="합성 시스템 지시"),
    Message(role="user", content=SYNTHETIC_QUESTION),
]


@contextmanager
def running_upstream(respond: Callable[[BaseHTTPRequestHandler], None]) -> Iterator[str]:
    """합성 vLLM. `respond`가 한 POST에 대한 응답 전체를 직접 쓴다."""

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # BaseHTTPRequestHandler가 정한 이름이라 그대로 쓴다
            length = int(self.headers.get("Content-Length", "0") or "0")
            # 본문을 먼저 비워야 클라이언트가 전송을 끝낼 수 있다. 확인이 필요한
            # 테스트는 BaseHTTPRequestHandler에 없는 이 속성으로 읽는다.
            self.body = self.rfile.read(length)  # type: ignore[attr-defined]
            respond(self)

        def log_message(self, *args: object) -> None:
            """테스트 출력에 접근 로그를 섞지 않는다."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def begin_stream(request: BaseHTTPRequestHandler) -> None:
    request.send_response(200)
    request.send_header("Content-Type", "text/event-stream")
    request.end_headers()


def write(request: BaseHTTPRequestHandler, text: str) -> None:
    request.wfile.write(text.encode("utf-8"))
    request.wfile.flush()


def delta(text: str) -> str:
    chunk = {"choices": [{"index": 0, "delta": {"content": text}}]}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def client_for(
    url: str,
    *,
    connect_timeout_seconds: float = 2.0,
    first_token_timeout_seconds: float = 5.0,
    idle_timeout_seconds: float = 5.0,
) -> VllmInferenceClient:
    return VllmInferenceClient(
        base_url=url,
        model="synthetic-model",
        connect_timeout_seconds=connect_timeout_seconds,
        first_token_timeout_seconds=first_token_timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
    )


def collect(client: VllmInferenceClient) -> list[str]:
    return list(client.start(uuid4(), MESSAGES, max_tokens=8))


# --- 1. 정상 경로 ---------------------------------------------------------


def test_content_deltas_become_tokens_and_done_ends_the_stream() -> None:
    def respond(request: BaseHTTPRequestHandler) -> None:
        begin_stream(request)
        write(request, delta("합성 "))
        write(request, delta("응답"))
        write(request, delta("조각"))
        write(request, "data: [DONE]\n\n")

    with running_upstream(respond) as url:
        client = client_for(url)
        assert collect(client) == ["합성 ", "응답", "조각"]
        client.close()


def test_request_body_carries_the_openai_messages_array() -> None:
    seen: list[dict[str, object]] = []

    def respond(request: BaseHTTPRequestHandler) -> None:
        # body는 위 running_upstream이 붙인 속성이다.
        seen.append(json.loads(request.body))  # type: ignore[attr-defined]
        begin_stream(request)
        write(request, "data: [DONE]\n\n")

    with running_upstream(respond) as url:
        client = client_for(url)
        collect(client)
        client.close()

    assert seen[0]["stream"] is True
    assert seen[0]["max_tokens"] == 8
    assert seen[0]["model"] == "synthetic-model"
    # 내부 Message가 OpenAI messages 배열로 그대로 옮겨진다.
    assert seen[0]["messages"] == [
        {"role": "system", "content": "합성 시스템 지시"},
        {"role": "user", "content": SYNTHETIC_QUESTION},
    ]


# --- 2. 무시해야 하는 이벤트 ----------------------------------------------


def test_keepalive_empty_delta_and_usage_events_are_ignored() -> None:
    def respond(request: BaseHTTPRequestHandler) -> None:
        begin_stream(request)
        write(request, ": keepalive\n\n")
        # role만 실린 첫 조각 — content가 없다.
        write(request, 'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n')
        write(request, delta("실"))
        write(request, 'data: {"choices":[{"index":0,"delta":{"content":""}}]}\n\n')
        # finish_reason만 실린 조각.
        write(request, 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n')
        # usage 전용 event — choices가 비어 있다.
        write(request, 'data: {"choices":[],"usage":{"total_tokens":3}}\n\n')
        write(request, delta("체"))
        write(request, "data: [DONE]\n\n")

    with running_upstream(respond) as url:
        client = client_for(url)
        assert collect(client) == ["실", "체"]
        client.close()


# --- 3. 비정상 HTTP 상태 --------------------------------------------------


@pytest.mark.parametrize("status", [401, 429, 500])
def test_unexpected_http_status_raises_without_leaking_body(status: int) -> None:
    secret_body = "프롬프트가-그대로-담긴-오류-본문"

    def respond(request: BaseHTTPRequestHandler) -> None:
        payload = json.dumps({"error": secret_body}).encode("utf-8")
        request.send_response(status)
        request.send_header("Content-Type", "application/json")
        request.send_header("Content-Length", str(len(payload)))
        request.end_headers()
        request.wfile.write(payload)

    with running_upstream(respond) as url:
        client = client_for(url)
        with pytest.raises(UpstreamError) as raised:
            collect(client)
        client.close()

    assert raised.value.code == f"upstream_status_{status}"
    # 상태 코드만 남고 본문은 어디에도 들어가지 않는다.
    assert secret_body not in str(raised.value)


# --- 4. 형식 오류 ---------------------------------------------------------


def test_malformed_json_is_not_reported_as_normal_completion() -> None:
    def respond(request: BaseHTTPRequestHandler) -> None:
        begin_stream(request)
        write(request, delta("앞"))
        write(request, "data: {이건 JSON이 아니다\n\n")
        write(request, "data: [DONE]\n\n")

    with running_upstream(respond) as url:
        client = client_for(url)
        iterator = client.start(uuid4(), MESSAGES, max_tokens=8)
        assert next(iterator) == "앞"
        with pytest.raises(UpstreamError) as raised:
            next(iterator)
        client.close()

    assert raised.value.code == "upstream_malformed_response"


def test_unexpected_chunk_shape_is_rejected() -> None:
    def respond(request: BaseHTTPRequestHandler) -> None:
        begin_stream(request)
        # content가 문자열이 아니다 — 조용히 무시하면 답변이 소리 없이 빠진다.
        write(request, 'data: {"choices":[{"index":0,"delta":{"content":123}}]}\n\n')
        write(request, "data: [DONE]\n\n")

    with running_upstream(respond) as url:
        client = client_for(url)
        with pytest.raises(UpstreamError) as raised:
            collect(client)
        client.close()

    assert raised.value.code == "upstream_malformed_response"


# --- 5·6. timeout 세 가지의 구분 -------------------------------------------


def test_first_token_timeout_while_only_keepalives_arrive() -> None:
    """keepalive는 바이트가 오므로 socket read timeout으로는 잡히지 않는다."""

    def respond(request: BaseHTTPRequestHandler) -> None:
        begin_stream(request)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                write(request, ": keepalive\n\n")
            except OSError:
                return
            time.sleep(0.02)

    with running_upstream(respond) as url:
        client = client_for(url, first_token_timeout_seconds=0.3, idle_timeout_seconds=0.3)
        with pytest.raises(UpstreamError) as raised:
            collect(client)
        client.close()

    assert raised.value.code == "upstream_first_token_timeout"


def test_first_token_timeout_when_the_socket_stays_silent() -> None:
    """반대 경우 — 아무 바이트도 오지 않으면 httpx read timeout이 먼저 끊는다."""

    def respond(request: BaseHTTPRequestHandler) -> None:
        begin_stream(request)
        time.sleep(3.0)

    with running_upstream(respond) as url:
        client = client_for(url, first_token_timeout_seconds=0.3, idle_timeout_seconds=0.3)
        with pytest.raises(UpstreamError) as raised:
            collect(client)
        client.close()

    assert raised.value.code == "upstream_first_token_timeout"


def test_idle_timeout_between_tokens() -> None:
    def respond(request: BaseHTTPRequestHandler) -> None:
        begin_stream(request)
        write(request, delta("첫"))
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                write(request, ": keepalive\n\n")
            except OSError:
                return
            time.sleep(0.02)

    with running_upstream(respond) as url:
        # 첫 토큰 한도는 넉넉하게 둬서 idle 쪽만 반응하는지 본다.
        client = client_for(url, first_token_timeout_seconds=5.0, idle_timeout_seconds=0.3)
        iterator = client.start(uuid4(), MESSAGES, max_tokens=8)
        assert next(iterator) == "첫"
        with pytest.raises(UpstreamError) as raised:
            next(iterator)
        client.close()

    assert raised.value.code == "upstream_idle_timeout"


def test_idle_timeout_holds_when_the_stream_goes_completely_silent() -> None:
    """keepalive조차 없는 경우. 분류가 아니라 **시각**을 단정하는 테스트다.

    socket read timeout 하나로만 막으면 그 값이 첫 토큰 한도(여기서 5초)를 따라가므로,
    idle 한도 0.3초가 아니라 5초 뒤에야 끊긴다. 그때도 분류는 upstream_idle_timeout이라
    코드만 보면 통과한다 — 그래서 경과 시간을 함께 본다.
    """

    def respond(request: BaseHTTPRequestHandler) -> None:
        begin_stream(request)
        write(request, delta("첫"))
        # 여기서부터 바이트를 한 개도 보내지 않는다.
        time.sleep(4.0)

    with running_upstream(respond) as url:
        client = client_for(url, first_token_timeout_seconds=5.0, idle_timeout_seconds=0.3)
        iterator = client.start(uuid4(), MESSAGES, max_tokens=8)
        assert next(iterator) == "첫"
        started = time.monotonic()
        with pytest.raises(UpstreamError) as raised:
            next(iterator)
        elapsed = time.monotonic() - started
        client.close()

    assert raised.value.code == "upstream_idle_timeout"
    assert elapsed < 2.0, f"idle 한도 0.3초가 아니라 {elapsed:.1f}초 뒤에 끊겼다"


def test_first_token_timeout_is_not_stretched_by_a_long_idle_timeout() -> None:
    """거울 사례 — 두 한도가 비대칭일 때 첫 토큰 쪽이 idle 값에 끌려가지 않는지 본다."""

    def respond(request: BaseHTTPRequestHandler) -> None:
        begin_stream(request)
        time.sleep(4.0)

    with running_upstream(respond) as url:
        client = client_for(url, first_token_timeout_seconds=0.3, idle_timeout_seconds=5.0)
        started = time.monotonic()
        with pytest.raises(UpstreamError) as raised:
            collect(client)
        elapsed = time.monotonic() - started
        client.close()

    assert raised.value.code == "upstream_first_token_timeout"
    assert elapsed < 2.0, f"첫 토큰 한도 0.3초가 아니라 {elapsed:.1f}초 뒤에 끊겼다"


# --- 7. [DONE] 없는 종료 ---------------------------------------------------


def test_stream_ending_without_done_is_reported_as_disconnect() -> None:
    def respond(request: BaseHTTPRequestHandler) -> None:
        begin_stream(request)
        write(request, delta("반쪽"))
        # [DONE] 없이 연결을 닫는다.

    with running_upstream(respond) as url:
        client = client_for(url)
        iterator = client.start(uuid4(), MESSAGES, max_tokens=8)
        assert next(iterator) == "반쪽"
        with pytest.raises(UpstreamError) as raised:
            next(iterator)
        client.close()

    # 조용한 정상 완료가 아니어야 한다 — 서비스가 completed로 기록해 버린다.
    assert raised.value.code == "upstream_disconnected"


# --- 8. 취소 --------------------------------------------------------------


def test_cancel_closes_upstream_and_ends_without_exception() -> None:
    write_failed = threading.Event()

    def respond(request: BaseHTTPRequestHandler) -> None:
        begin_stream(request)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                write(request, delta("조각"))
            except OSError:
                # 클라이언트가 연결을 닫았다 — 취소가 실제로 소켓까지 닿았다는 증거다.
                write_failed.set()
                return
            time.sleep(0.02)

    with running_upstream(respond) as url:
        client = client_for(url)
        generation_id = uuid4()
        tokens: list[str] = []
        raised: list[BaseException] = []

        def consume() -> None:
            # 서비스 계층과 같은 모양 — 워커 스레드가 돌고 cancel은 다른 스레드에서 온다.
            try:
                # list()로 한 번에 모으면 메인 스레드가 "첫 토큰이 왔다"를 볼 수 없다.
                # 취소를 걸 시점을 잡으려면 하나씩 쌓아야 한다.
                for token in client.start(generation_id, MESSAGES, max_tokens=64):
                    tokens.append(token)  # noqa: PERF402
            except BaseException as error:  # noqa: BLE001 - 어떤 예외든 실패로 본다
                raised.append(error)

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        first_token_deadline = time.monotonic() + 3.0
        while not tokens and time.monotonic() < first_token_deadline:
            time.sleep(0.01)
        assert tokens, "취소 전에 토큰이 하나는 와야 이 테스트가 의미 있다"

        client.cancel(generation_id)
        worker.join(timeout=5.0)
        client.close()

    assert not worker.is_alive()
    # 취소는 실패가 아니다 — 예외 없이 끝나야 한다(Protocol 계약).
    assert raised == []
    assert write_failed.wait(timeout=3.0), "취소가 업스트림 연결을 닫지 않았다"


def test_cancel_before_the_first_iteration_is_not_lost() -> None:
    """start()가 제너레이터면 등록이 첫 next()까지 밀려 이 취소가 유실된다.

    서비스는 start()를 부른 뒤 워커 스레드에서 iteration하므로 그 사이에 취소·시간 초과가
    올 수 있다. 등록이 호출 시점에 끝나야 cancel()이 이 attempt를 찾는다.
    """
    requests: list[str] = []

    def respond(request: BaseHTTPRequestHandler) -> None:
        requests.append(request.path)
        begin_stream(request)
        write(request, delta("보내면 안 되는 조각"))
        write(request, "data: [DONE]\n\n")

    with running_upstream(respond) as url:
        client = client_for(url)
        generation_id = uuid4()
        iterator = client.start(generation_id, MESSAGES, max_tokens=8)
        client.cancel(generation_id)
        # 취소는 실패가 아니다 — 예외 없이 빈 결과로 끝난다.
        assert list(iterator) == []
        client.close()

    # 취소가 유실되지 않았다면 업스트림에 요청 자체가 가지 않는다.
    assert requests == []


def test_cancel_is_idempotent_for_unknown_and_finished_generations() -> None:
    def respond(request: BaseHTTPRequestHandler) -> None:
        begin_stream(request)
        write(request, "data: [DONE]\n\n")

    with running_upstream(respond) as url:
        client = client_for(url)
        generation_id = uuid4()
        # 모르는 id
        client.cancel(generation_id)
        assert collect(client) == []
        # 이미 끝난 id — 두 번 불러도 조용하다.
        client.cancel(generation_id)
        client.cancel(generation_id)
        client.close()


# --- 9. mock으로 fallback하지 않는다 ---------------------------------------


def llm_settings(base_url: str) -> Settings:
    return Settings(
        DATABASE_URL="postgresql://unused",
        PERSONA_EMBEDDING_URL="http://embedding.invalid",
        PERSONA_STATIC_BEARER_TOKEN="synthetic-token",
        PERSONA_STATIC_USER_ID="synthetic-user",
        PERSONA_STATIC_DISPLAY_NAME="합성 사용자",
        PERSONA_CURSOR_SIGNING_KEY="synthetic-cursor-key",
        PERSONA_CHAT_INFERENCE_MODE="llm",
        PERSONA_VLLM_BASE_URL=base_url,
        PERSONA_VLLM_MODEL="synthetic-model",
    )


def test_llm_failure_does_not_turn_into_a_mock_answer() -> None:
    def respond(request: BaseHTTPRequestHandler) -> None:
        request.send_response(503)
        request.send_header("Content-Length", "0")
        request.end_headers()

    with running_upstream(respond) as url:
        client = build_inference_client(llm_settings(url))
        assert not isinstance(client, FakeInferenceClient)
        tokens: list[str] = []
        with pytest.raises(UpstreamError) as raised:
            tokens.extend(client.start(uuid4(), MESSAGES, max_tokens=8))

    assert raised.value.code == "upstream_status_503"
    # 실패가 합성 응답으로 바뀌지 않는다.
    assert tokens == []
    for synthetic in DEFAULT_CHUNKS:
        assert synthetic not in "".join(tokens)


def test_build_inference_client_picks_the_adapter_from_settings() -> None:
    mock_settings = llm_settings("http://unused.invalid").model_copy(
        update={"chat_inference_mode": "mock"}
    )
    assert isinstance(build_inference_client(mock_settings), FakeInferenceClient)
    llm_client = build_inference_client(llm_settings("http://unused.invalid"))
    assert isinstance(llm_client, VllmInferenceClient)
    llm_client.close()


# --- 비유출 경계 -----------------------------------------------------------


def test_connection_failure_message_does_not_contain_url_or_prompt() -> None:
    # 아무도 듣지 않는 포트.
    client = client_for("http://127.0.0.1:1", connect_timeout_seconds=0.5)
    with pytest.raises(UpstreamError) as raised:
        collect(client)
    client.close()

    assert raised.value.code == "upstream_unavailable"
    message = str(raised.value)
    assert "127.0.0.1" not in message
    assert SYNTHETIC_QUESTION not in message


def test_api_key_is_sent_as_a_header_and_never_appears_in_errors() -> None:
    seen: list[str] = []

    def respond(request: BaseHTTPRequestHandler) -> None:
        seen.append(request.headers.get("Authorization", ""))
        request.send_response(500)
        request.send_header("Content-Length", "0")
        request.end_headers()

    with running_upstream(respond) as url:
        client = VllmInferenceClient(
            base_url=url,
            model="synthetic-model",
            api_key=SYNTHETIC_API_KEY,
            connect_timeout_seconds=2.0,
            first_token_timeout_seconds=2.0,
            idle_timeout_seconds=2.0,
        )
        with pytest.raises(UpstreamError) as raised:
            collect(client)
        client.close()

    assert seen[0] == f"Bearer {SYNTHETIC_API_KEY}"
    assert SYNTHETIC_API_KEY not in str(raised.value)
