"""검색 지연 관측용 Prometheus 지표. `/metrics`(main.py)가 이 레지스트리를 노출한다."""

from __future__ import annotations

from prometheus_client import Histogram

RETRIEVAL_SECONDS = Histogram(
    "persona_retrieval_seconds",
    "search() 호출 지연 — 본문/대사 검색을 kind_group으로 구분해 관찰한다.",
    labelnames=("kind_group",),
)
