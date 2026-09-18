"""SentenceTransformer 모델을 프로세스당 한 번만 올려 쥐고 있는다.

우비콘 워커를 1개로 고정하는 것(Dockerfile)과 짝을 이룬다 — 워커가 여러 개면
프로세스마다 이 클래스를 새로 만들어 모델을 중복 로딩하게 된다.
"""

from __future__ import annotations

import logging

# config를 먼저 import해 OMP_NUM_THREADS가 torch보다 먼저 정해지게 한다.
from . import config

logger = logging.getLogger(__name__)


class Model:
    """`load()`가 끝나기 전에는 `ready`가 거짓이고 `encode()`를 호출하면 안 된다 —
    호출 쪽(main.py)이 `/readyz`로 이 상태를 노출한다."""

    def __init__(self) -> None:
        self._model = None

    def load(self) -> None:
        # 지연 import: 모듈 최상단에서 sentence_transformers를 끌어오면 config의
        # OMP_NUM_THREADS 설정보다 먼저 torch가 초기화될 여지가 생긴다.
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(
            config.MODEL_NAME, revision=config.MODEL_REVISION, device="cpu"
        )
        logger.info("model loaded name=%s revision=%s", config.MODEL_NAME, config.MODEL_REVISION)

    @property
    def ready(self) -> bool:
        return self._model is not None

    def encode(self, texts: list[str]) -> list[list[float]]:
        """입력 순서를 보존한 L2 정규화 벡터를 돌려준다. `ready`가 거짓일 때 호출하면
        `RuntimeError` — 호출 쪽이 `/readyz` 없이 `/embed`를 받아버린 버그다."""
        if self._model is None:
            raise RuntimeError("model.encode called before load() finished")
        vectors = self._model.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False
        )
        return vectors.tolist()
