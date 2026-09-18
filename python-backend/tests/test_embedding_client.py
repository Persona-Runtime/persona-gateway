"""embedding_client.py 단위 테스트 — 실제 네트워크 없이 httpx.MockTransport로."""

from __future__ import annotations

import json

import httpx
import pytest

from persona_minimal_api.indexing.embedding_client import (
    BATCH_SIZE,
    EmbeddingError,
    EmbeddingResult,
    embed,
)


def _install(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    """`httpx.Client(...)`가 이 transport를 쓰도록 한다 — embed()는 base_url만 받고
    Client를 직접 만들므로, Client 생성 자체를 가로챈다."""

    real_client = httpx.Client

    def client_factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_factory)


def test_embed_splits_into_batches_and_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        texts = payload["texts"]
        calls.append(texts)
        vectors = [[float(len(t))] for t in texts]
        return httpx.Response(200, json={"model": "m@rev", "dim": 1, "vectors": vectors})

    _install(monkeypatch, httpx.MockTransport(handler))
    texts = [f"문장{i}" for i in range(40)]

    result = embed("http://embedding.local", texts, "passage")

    assert len(calls) == 2
    assert len(calls[0]) == BATCH_SIZE
    assert len(calls[1]) == 40 - BATCH_SIZE
    assert result == EmbeddingResult(model="m@rev", vectors=[[float(len(t))] for t in texts])


def test_embed_retries_once_on_connect_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("연결 안 됨", request=request)
        return httpx.Response(200, json={"model": "m@rev", "dim": 1, "vectors": [[1.0]]})

    _install(monkeypatch, httpx.MockTransport(handler))

    result = embed("http://embedding.local", ["문장"], "query")

    assert attempts["n"] == 2
    assert result == EmbeddingResult(model="m@rev", vectors=[[1.0]])


def test_embed_gives_up_after_one_retry_on_repeated_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("연결 안 됨", request=request)

    _install(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(EmbeddingError):
        embed("http://embedding.local", ["문장"], "query")

    # 최초 시도 + 재시도 1회 = 2번. 더 재시도하지 않는다.
    assert attempts["n"] == 2


def test_embed_does_not_retry_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ReadTimeout("응답이 늦음", request=request)

    _install(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(EmbeddingError):
        embed("http://embedding.local", ["문장"], "query")

    # 연결은 됐고 응답만 늦은 경우라 재시도 대상이 아니다.
    assert attempts["n"] == 1


def test_embed_does_not_retry_http_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, json={"error": "model not loaded"})

    _install(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(EmbeddingError):
        embed("http://embedding.local", ["문장"], "query")

    assert attempts["n"] == 1


def test_embed_error_message_does_not_contain_input_text(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_text = "실제-원문-절대-로그에-남으면-안-됨"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결 안 됨", request=request)

    _install(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(EmbeddingError) as excinfo:
        embed("http://embedding.local", [secret_text], "passage")

    assert secret_text not in str(excinfo.value)


def test_embed_rejects_invalid_input_type() -> None:
    with pytest.raises(ValueError, match="passage/query"):
        embed("http://embedding.local", ["문장"], "bogus")  # type: ignore[arg-type]


def test_embed_returns_empty_list_without_calling_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("빈 입력이면 서버를 부르면 안 된다")

    _install(monkeypatch, httpx.MockTransport(handler))

    assert embed("http://embedding.local", [], "passage") == EmbeddingResult(model=None, vectors=[])


def test_embed_rejects_inconsistent_model_across_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        payload = json.loads(request.content)
        model = "m@rev1" if calls["n"] == 1 else "m@rev2"
        vectors = [[1.0]] * len(payload["texts"])
        return httpx.Response(200, json={"model": model, "dim": 1, "vectors": vectors})

    _install(monkeypatch, httpx.MockTransport(handler))
    texts = [f"문장{i}" for i in range(40)]  # 32 + 8, 두 배치를 강제한다

    with pytest.raises(EmbeddingError, match="다른 모델"):
        embed("http://embedding.local", texts, "passage")
