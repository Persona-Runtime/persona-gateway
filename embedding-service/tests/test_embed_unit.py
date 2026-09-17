"""§4-4 단위 테스트 — 실제 모델 없이 얇은 stub으로. 실제 모델은
test_embed_integration.py(marker: integration)에서 1개만 확인한다."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from persona_embedding_service import main as main_module
from persona_embedding_service.config import (
    EMBEDDING_DIM,
    MAX_CHARS_PER_TEXT,
    MAX_TEXTS_PER_REQUEST,
)
from persona_embedding_service.main import create_app
from persona_embedding_service.model import Model


class _StubModel(Model):
    """실제 SentenceTransformer 대신 텍스트 길이를 벡터 첫 성분에 담는 결정적 함수 —
    "같은 텍스트는 같은 벡터, 순서는 보존"을 검증하려고 이 성질만 있으면 된다."""

    def __init__(self, ready: bool = True) -> None:
        super().__init__()
        self._ready = ready
        self.calls: list[list[str]] = []

    @property
    def ready(self) -> bool:
        return self._ready

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[float(len(text))] + [0.0] * (EMBEDDING_DIM - 1) for text in texts]


def _client(ready: bool = True) -> tuple[TestClient, _StubModel]:
    stub = _StubModel(ready=ready)
    return TestClient(create_app(model=stub)), stub


def test_embed_prefixes_passage_and_query_differently() -> None:
    client, stub = _client()
    client.post("/embed", json={"input_type": "passage", "texts": ["안녕"]})
    client.post("/embed", json={"input_type": "query", "texts": ["안녕"]})
    assert stub.calls[0] == ["passage: 안녕"]
    assert stub.calls[1] == ["query: 안녕"]


def test_embed_preserves_order_and_repeats_same_vector_for_same_text() -> None:
    client, _ = _client()
    response = client.post(
        "/embed", json={"input_type": "passage", "texts": ["가", "다른 길이 문장", "가"]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["dim"] == EMBEDDING_DIM
    assert len(body["vectors"]) == 3
    assert body["vectors"][0] == body["vectors"][2]
    assert body["vectors"][0] != body["vectors"][1]
    assert body["model"].startswith("multilingual-e5-small@")


def test_embed_rejects_more_than_max_texts() -> None:
    client, _ = _client()
    texts = ["합성 문장"] * (MAX_TEXTS_PER_REQUEST + 1)

    response = client.post("/embed", json={"input_type": "passage", "texts": texts})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_embed_accepts_exactly_max_texts() -> None:
    client, _ = _client()
    texts = ["합성 문장"] * MAX_TEXTS_PER_REQUEST

    response = client.post("/embed", json={"input_type": "passage", "texts": texts})

    assert response.status_code == 200
    assert len(response.json()["vectors"]) == MAX_TEXTS_PER_REQUEST


def test_embed_rejects_text_over_char_limit() -> None:
    client, _ = _client()
    texts = ["a" * (MAX_CHARS_PER_TEXT + 1)]

    response = client.post("/embed", json={"input_type": "passage", "texts": texts})

    assert response.status_code == 422


def test_embed_accepts_text_at_char_limit() -> None:
    client, _ = _client()
    texts = ["a" * MAX_CHARS_PER_TEXT]

    response = client.post("/embed", json={"input_type": "passage", "texts": texts})

    assert response.status_code == 200


def test_embed_rejects_empty_string() -> None:
    client, _ = _client()

    response = client.post("/embed", json={"input_type": "passage", "texts": [""]})

    assert response.status_code == 422


def test_embed_rejects_invalid_input_type() -> None:
    client, _ = _client()

    response = client.post("/embed", json={"input_type": "bogus", "texts": ["안녕"]})

    assert response.status_code == 422


def test_embed_returns_503_when_model_not_ready() -> None:
    client, _ = _client(ready=False)

    response = client.post("/embed", json={"input_type": "passage", "texts": ["안녕"]})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_not_ready"


def test_readyz_reflects_model_state() -> None:
    not_ready_client, _ = _client(ready=False)
    assert not_ready_client.get("/readyz").status_code == 503

    ready_client, _ = _client(ready=True)
    assert ready_client.get("/readyz").status_code == 200


def test_healthz_is_always_ok_regardless_of_model_state() -> None:
    client, _ = _client(ready=False)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_load_done_callback_exits_process_on_load_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """asyncio.create_task로 시작한 로딩 태스크가 예외로 끝나면 그 예외가 태스크
    안에 갇혀 조용히 사라진다 — /readyz가 로그 한 줄 없이 영원히 503만 내는 걸
    막으려고 콜백이 프로세스를 종료해야 한다."""
    exit_codes: list[int] = []
    monkeypatch.setattr(main_module.os, "_exit", exit_codes.append)

    async def _failing_load() -> None:
        raise RuntimeError("synthetic model load failure")

    async def _run() -> None:
        task = asyncio.create_task(_failing_load())
        with pytest.raises(RuntimeError):
            await task
        main_module._on_load_done(task)

    asyncio.run(_run())

    assert exit_codes == [1]


def test_load_done_callback_does_nothing_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_codes: list[int] = []
    monkeypatch.setattr(main_module.os, "_exit", exit_codes.append)

    async def _run() -> None:
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        main_module._on_load_done(task)

    asyncio.run(_run())

    assert exit_codes == []
