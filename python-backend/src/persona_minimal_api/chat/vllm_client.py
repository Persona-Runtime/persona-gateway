"""vLLM의 OpenAI 호환 streaming API를 부르는 `InferenceClient` 구현(llm 모드).

`FakeInferenceClient`와 같은 계약을 지킨다 — 동기 `Iterator[str]`을 돌리고, 취소는
예외 없이 조용히 끝내며, 그 외 비정상 종료는 `UpstreamError`를 던진다. 서비스 계층이
이 이터레이터를 **daemon 워커 스레드**에서 돌리고 `cancel()`은 다른 스레드(FastAPI
threadpool)에서 부르므로, 여기 상태는 잠금으로 보호한다.

책임 경계:

- 시간 예산(첫 답변 60초·전체 180초)은 **서비스 계층**이 갖는다(`chat/service.py`).
  여기 timeout은 그보다 짧은 **소켓·스트림 보호**이고, 사용자에게 보이는 상한을 정하지
  않는다.
- 프롬프트 조립(RAG·예산)은 `retrieval/prompt.py`가 이미 끝냈다. 여기서는 내부 `Message`를
  OpenAI messages 배열로 옮기기만 한다.

기록하지 않는 것(의도적): 프롬프트·응답 원문·응답 헤더·URL·API key. 오류에는 고정 분류
문자열과 HTTP 상태 코드만 넣는다 — 그 문자열이 그대로 generation의 `failure_code`와 SSE
error 이벤트의 `code`가 되어 DB·클라이언트·로그에 남기 때문이다.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from uuid import UUID

import httpx

from ..retrieval.prompt import Message
from .inference import UpstreamError

logger = logging.getLogger(__name__)

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
DONE_SENTINEL = "[DONE]"
_DATA_PREFIX = "data:"

# upstream 실패 분류. 이 문자열이 그대로 failure_code가 되므로 원문을 섞지 않는다.
OUTCOME_UNAVAILABLE = "upstream_unavailable"
OUTCOME_FIRST_TOKEN_TIMEOUT = "upstream_first_token_timeout"
OUTCOME_IDLE_TIMEOUT = "upstream_idle_timeout"
OUTCOME_MALFORMED = "upstream_malformed_response"
# fake와 같은 code를 쓴다 — 서비스 계층이 둘을 구분할 이유가 없고, 운영에서 보는 실패
# 분류가 어댑터마다 갈라지면 대시보드도 갈라진다.
OUTCOME_DISCONNECTED = "upstream_disconnected"


class _StreamDone:
    """`[DONE]` 표식. 빈 문자열 토큰과 구분하려고 별도 타입을 쓴다."""


_STREAM_DONE = _StreamDone()


class _Attempt:
    """generation 하나의 진행 상태. `start()`의 워커 스레드와 `cancel()`이 함께 본다."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.response: httpx.Response | None = None
        # 첫 content 조각을 봤는지 — read timeout을 "첫 토큰 대기"와 "토큰 사이 무응답"
        # 중 어느 쪽으로 분류할지가 이 값에 달려 있다.
        self.saw_token = False


class VllmInferenceClient:
    """vLLM OpenAI 호환 `/v1/chat/completions`(stream=true)를 토큰 스트림으로 바꾼다."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        connect_timeout_seconds: float,
        first_token_timeout_seconds: float,
        idle_timeout_seconds: float,
        client: httpx.Client | None = None,
    ):
        self._url = f"{base_url.rstrip('/')}{CHAT_COMPLETIONS_PATH}"
        self._model = model
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._first_token_timeout = first_token_timeout_seconds
        self._idle_timeout = idle_timeout_seconds
        # httpx의 read timeout은 "바이트가 오지 않는 시간"이다. 아래에서 직접 재는 두
        # 한도는 "content 조각이 오지 않는 시간"이라 역할이 다르다 — keepalive가 계속
        # 오면 직접 재는 쪽만 반응하고, 서버가 완전히 침묵하면 iter_lines()가 블록돼
        # 직접 재는 쪽이 돌 기회조차 없으므로 httpx 쪽만 반응한다. 둘 다 필요하다.
        # 값은 두 한도 중 큰 쪽으로 둬서 socket timeout이 먼저 터지지 않게 한다.
        socket_read_timeout = max(first_token_timeout_seconds, idle_timeout_seconds)
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(
                connect=connect_timeout_seconds,
                read=socket_read_timeout,
                write=connect_timeout_seconds,
                pool=connect_timeout_seconds,
            )
        )
        self._attempts: dict[UUID, _Attempt] = {}
        self._lock = threading.Lock()

    # --- InferenceClient 계약 ----------------------------------------------

    def start(
        self, generation_id: UUID, messages: list[Message], *, max_tokens: int
    ) -> Iterator[str]:
        attempt = self._register(generation_id)
        try:
            yield from self._stream(attempt, messages, max_tokens)
        finally:
            # 끝난 generation의 상태는 지운다 — 남겨 두면 프로세스 수명 동안 계속 쌓이고,
            # 뒤늦게 같은 id로 오는 cancel은 어차피 무시해도 되는 호출이다.
            self._forget(generation_id)

    def cancel(self, generation_id: UUID) -> None:
        """멱등. 모르는/이미 끝난 id는 조용히 무시한다.

        플래그만 세우지 않고 **열린 응답을 닫는다.** 워커 스레드는 `iter_lines()` 안에서
        블록돼 있어 플래그를 볼 기회가 없고, 서비스는 시간 초과 때 워커를 join하지 않고
        바로 다음으로 넘어간다 — 여기서 닫지 않으면 daemon 스레드가 소켓을 쥔 채 남는다.
        """
        with self._lock:
            attempt = self._attempts.get(generation_id)
        if attempt is None:
            return
        attempt.cancelled.set()
        response = attempt.response
        if response is None:
            return
        try:
            response.close()
        except Exception as error:  # noqa: BLE001 - 닫기 실패가 취소를 막으면 안 된다
            # 이미 닫혔거나 닫는 중일 수 있다. 취소는 멱등이므로 실패로 만들지 않는다.
            logger.debug("upstream 응답 닫기 실패(%s)", type(error).__name__)

    def close(self) -> None:
        """앱 종료 시 HTTP 연결 풀을 정리한다."""
        self._client.close()

    # --- 내부 --------------------------------------------------------------

    def _register(self, generation_id: UUID) -> _Attempt:
        attempt = _Attempt()
        with self._lock:
            self._attempts[generation_id] = attempt
        return attempt

    def _forget(self, generation_id: UUID) -> None:
        with self._lock:
            self._attempts.pop(generation_id, None)

    def _stream(self, attempt: _Attempt, messages: list[Message], max_tokens: int) -> Iterator[str]:
        payload = {
            "model": self._model,
            # 내부 Message를 OpenAI messages 배열로 명시적으로 옮긴다. dataclass를 통째로
            # 직렬화하지 않는 이유: 나중에 Message에 필드가 늘어도 프롬프트가 아닌 값이
            # 실수로 upstream에 나가지 않는다.
            "messages": [{"role": item.role, "content": item.content} for item in messages],
            "max_tokens": max_tokens,
            "stream": True,
        }
        started = time.monotonic()
        try:
            with self._client.stream(
                "POST", self._url, json=payload, headers=self._headers
            ) as response:
                attempt.response = response
                if attempt.cancelled.is_set():
                    # 응답을 붙이기 전에 취소가 왔다 — cancel()이 닫을 대상이 없었으므로
                    # 여기서 끝낸다(예외 아님).
                    return
                if response.status_code != 200:
                    # 본문을 읽지 않는다 — vLLM 오류 본문에 프롬프트가 그대로 담겨 오는
                    # 경우가 있고, 그게 failure_code·로그로 흘러가면 안 된다.
                    raise UpstreamError(f"upstream_status_{response.status_code}")
                yield from self._tokens(attempt, response, started)
        except UpstreamError:
            raise
        except httpx.ReadTimeout as error:
            # 요청은 갔는데 서버가 침묵했다. 첫 조각 전인지 후인지로 분류가 갈린다 —
            # "연결이 안 된다"와 "답이 느리다"는 운영에서 다른 조치를 부른다.
            if attempt.cancelled.is_set():
                return
            code = OUTCOME_IDLE_TIMEOUT if attempt.saw_token else OUTCOME_FIRST_TOKEN_TIMEOUT
            raise UpstreamError(code) from error
        except httpx.HTTPError as error:
            # 취소로 응답을 닫으면 여기로 읽기 오류가 올라온다 — 그건 실패가 아니다.
            if attempt.cancelled.is_set():
                return
            # 예외 객체를 로그·메시지에 넣지 않는다(URL이 담겨 있다). 분류만 남긴다.
            logger.warning("vLLM 호출 실패(%s)", type(error).__name__)
            raise UpstreamError(OUTCOME_UNAVAILABLE) from error

    def _tokens(self, attempt: _Attempt, response: httpx.Response, started: float) -> Iterator[str]:
        """SSE 줄을 토큰으로 바꾼다. `[DONE]`이 정상 종료의 유일한 표시다."""
        last_token_at = started
        for line in response.iter_lines():
            if attempt.cancelled.is_set():
                # 취소 — 남은 조각을 만들지 않고 조용히 끝낸다(예외 아님).
                return
            self._check_content_deadline(attempt, started, last_token_at)
            token = self._token_from_line(line)
            if isinstance(token, _StreamDone):
                return
            if token is None:
                # keepalive·빈 delta·usage 전용 event·주석 줄. 정상이므로 무시한다.
                continue
            attempt.saw_token = True
            last_token_at = time.monotonic()
            yield token
        if attempt.cancelled.is_set():
            return
        # 루프가 예외 없이 끝났는데 [DONE]을 못 봤다 — upstream이 중간에 끊겼다는 뜻이다.
        # 여기서 조용히 return하면 서비스가 정상 완료로 기록한다(Protocol 계약).
        raise UpstreamError(OUTCOME_DISCONNECTED)

    def _check_content_deadline(
        self, attempt: _Attempt, started: float, last_token_at: float
    ) -> None:
        """content 조각 기준의 두 한도. socket read timeout과 달리 keepalive로 늘어나지 않는다."""
        now = time.monotonic()
        if not attempt.saw_token:
            if now - started > self._first_token_timeout:
                raise UpstreamError(OUTCOME_FIRST_TOKEN_TIMEOUT)
            return
        if now - last_token_at > self._idle_timeout:
            raise UpstreamError(OUTCOME_IDLE_TIMEOUT)

    def _token_from_line(self, line: str) -> str | _StreamDone | None:
        """SSE 한 줄에서 토큰을 꺼낸다.

        반환: 토큰 문자열 / `[DONE]`이면 `_STREAM_DONE` / 무시할 줄이면 None.
        """
        text = line.strip()
        if not text or text.startswith(":"):
            return None
        if not text.startswith(_DATA_PREFIX):
            # event:·id: 같은 다른 SSE 필드는 OpenAI 호환 스트림에서 의미가 없다.
            return None
        data = text[len(_DATA_PREFIX) :].strip()
        if data == DONE_SENTINEL:
            return _STREAM_DONE
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as error:
            # 본문을 메시지에 넣지 않는다 — 깨진 조각에도 응답 원문이 들어 있다.
            raise UpstreamError(OUTCOME_MALFORMED) from error
        return self._content_from_chunk(parsed)

    def _content_from_chunk(self, parsed: object) -> str | None:
        """OpenAI chunk에서 `choices[0].delta.content`만 꺼낸다. 없으면 None(무시)."""
        if not isinstance(parsed, dict):
            raise UpstreamError(OUTCOME_MALFORMED)
        choices = parsed.get("choices")
        if not choices:
            # usage 전용 event처럼 choices가 없거나 빈 조각 — 정상이므로 무시한다.
            return None
        if not isinstance(choices, list) or not isinstance(choices[0], dict):
            raise UpstreamError(OUTCOME_MALFORMED)
        delta = choices[0].get("delta")
        if delta is None:
            # finish_reason만 실린 마지막 조각. 종료는 [DONE]으로만 판단하므로 무시한다.
            return None
        if not isinstance(delta, dict):
            raise UpstreamError(OUTCOME_MALFORMED)
        content = delta.get("content")
        if content is None or content == "":
            # role만 실린 첫 조각이나 빈 delta.
            return None
        if not isinstance(content, str):
            raise UpstreamError(OUTCOME_MALFORMED)
        return content
