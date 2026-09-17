"""실제 sentence-transformers 모델을 1개 로딩해 §4-4 계약(384차원, L2 정규화)을
확인한다. fixture는 합성 캐릭터 "모루"만 쓴다(§5 — 실제 캐릭터 텍스트 금지)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from persona_embedding_service.main import create_app
from persona_embedding_service.model import Model

pytestmark = pytest.mark.integration

_MORU_TEXTS = [
    "합성 캐릭터 모루. 침착한 도서관 안내자다.",
    "개관 첫날 분실된 지도책을 찾아냈다.",
    "길을 묻는 사람에게: 차근차근 같이 찾아볼까요?",
]


@pytest.fixture(scope="module")
def client() -> TestClient:
    model = Model()
    model.load()  # 동기로 미리 올려 테스트 안에서 로딩 대기를 겪지 않는다.
    return TestClient(create_app(model=model))


def test_readyz_is_ok_once_model_loaded(client: TestClient) -> None:
    assert client.get("/readyz").status_code == 200


def test_embed_returns_normalized_384_dim_vectors_in_order(client: TestClient) -> None:
    response = client.post("/embed", json={"input_type": "passage", "texts": _MORU_TEXTS})

    assert response.status_code == 200
    body = response.json()
    assert body["dim"] == 384
    assert body["model"].startswith("multilingual-e5-small@")
    assert len(body["vectors"]) == len(_MORU_TEXTS)
    for vector in body["vectors"]:
        assert len(vector) == 384
        norm = sum(component**2 for component in vector) ** 0.5
        assert abs(norm - 1.0) < 1e-3


def test_embed_query_and_passage_prefixes_yield_different_vectors(client: TestClient) -> None:
    text = _MORU_TEXTS[0]
    passage = client.post("/embed", json={"input_type": "passage", "texts": [text]})
    query = client.post("/embed", json={"input_type": "query", "texts": [text]})

    assert passage.json()["vectors"][0] != query.json()["vectors"][0]
