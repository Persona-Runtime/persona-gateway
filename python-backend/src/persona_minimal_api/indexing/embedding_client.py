"""임베딩 서비스 HTTP 클라이언트 — 인수인계 §4-4.

호출자(`runner.py`)가 배치·재시도를 신경 쓰지 않도록 이 모듈이 32개씩 나눠 부르고
실패를 감싼다. 원문·응답 본문은 어디에도 남기지 않는다 — 예외 메시지에도, 로그에도.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

BATCH_SIZE = 32
TIMEOUT_SECONDS = 30.0

InputType = Literal["passage", "query"]


class EmbeddingError(Exception):
    """임베딩 서비스 호출 실패. 메시지에 입력 텍스트·응답 본문을 담지 않는다."""


@dataclass(frozen=True)
class EmbeddingResult:
    """§4-4 응답에서 꺼낸 값. `model`은 material_chunks.embedding_model에 그대로 쓴다 —

    호출자가 어떤 모델을 기대했는지가 아니라 서버가 실제로 무엇으로 계산했는지를
    저장해야 하므로, 클라이언트가 상수를 지어내지 않고 응답값을 그대로 돌려준다.
    """

    model: str | None
    vectors: list[list[float]]


def embed(base_url: str, texts: list[str], input_type: InputType) -> EmbeddingResult:
    """`texts`를 §4-4 계약대로 임베딩하고 입력 순서 그대로 벡터를 돌려준다.

    서버는 요청당 최대 64개를 받지만, 여기서는 32개씩 나눠 보낸다(계약이 정한 배치
    크기). 빈 입력이면 서버를 부르지 않고 `model=None`인 빈 결과를 돌려준다.
    """
    if input_type not in ("passage", "query"):
        raise ValueError(f"input_type은 passage/query만 허용한다: {input_type!r}")
    if not texts:
        return EmbeddingResult(model=None, vectors=[])
    model: str | None = None
    vectors: list[list[float]] = []
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start : start + BATCH_SIZE]
            batch_model, batch_vectors = _embed_batch(client, base_url, batch, input_type)
            if model is not None and batch_model != model:
                # 같은 embed() 호출 안에서 배치마다 서버가 다른 모델을 답하면, 한
                # material_chunks 저장에 서로 다른 embedding_model이 섞여 들어간다.
                raise EmbeddingError("임베딩 서비스가 배치마다 다른 모델을 돌려줬다")
            model = batch_model
            vectors.extend(batch_vectors)
    return EmbeddingResult(model=model, vectors=vectors)


def _embed_batch(
    client: httpx.Client, base_url: str, batch: list[str], input_type: InputType
) -> tuple[str, list[list[float]]]:
    payload = {"input_type": input_type, "texts": batch}
    try:
        response = _post_with_retry(client, base_url, payload)
    except httpx.HTTPError as exc:
        # exc 자체에도 요청 본문은 없다 — httpx 예외는 URL/타임아웃 정보만 담는다.
        raise EmbeddingError(f"임베딩 서비스 호출 실패: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise EmbeddingError(f"임베딩 서비스가 {response.status_code}를 돌려줬다")
    data = response.json()
    vectors = data["vectors"]
    if len(vectors) != len(batch):
        raise EmbeddingError("임베딩 개수가 입력 개수와 다르다")
    return data["model"], vectors


def _post_with_retry(
    client: httpx.Client, base_url: str, payload: dict[str, object]
) -> httpx.Response:
    """연결 자체가 안 되는 경우만 한 번 재시도한다.

    4xx/5xx 응답은 서버가 실제로 답한 것이므로 재시도 대상이 아니다(422는 입력이
    잘못됐고, 503은 모델 미로딩 — 둘 다 곧바로 다시 불러도 같은 결과다).
    `ReadTimeout`도 연결은 됐고 응답이 늦은 것이라 여기서는 재시도하지 않는다.
    """
    try:
        return client.post(f"{base_url}/embed", json=payload)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return client.post(f"{base_url}/embed", json=payload)
