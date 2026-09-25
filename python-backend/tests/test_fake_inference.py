"""mock workload profile 단위 테스트(DB 없음).

규칙: profile은 고정 합성 조각의 개수와 조각 사이 지연으로만 정의된다. 기본 short는 기존
기본 mock 응답과 같고, 잘못된 값은 조용히 보정되지 않고 생성 시점에 실패한다. 지연 검증은
실제 profile(최대 40초)이 아니라 같은 규칙으로 작게 줄인 profile로 한다.
"""

from __future__ import annotations

import math
import time
from uuid import uuid4

import pytest

from persona_minimal_api.chat.fake_inference import (
    DEFAULT_CHUNKS,
    MOCK_WORKLOAD_PROFILES,
    FakeInferenceClient,
    MockWorkloadProfile,
)


def test_profile_table_values_are_fixed() -> None:
    table = {
        name: (profile.fragment_count, profile.fragment_interval_seconds)
        for name, profile in MOCK_WORKLOAD_PROFILES.items()
    }

    assert table == {"short": (3, 0.0), "medium": (10, 0.5), "long": (80, 0.5)}


def test_short_profile_matches_existing_default_mock() -> None:
    short = FakeInferenceClient.from_profile(MOCK_WORKLOAD_PROFILES["short"])

    assert list(short.start(uuid4(), [], max_tokens=512)) == list(DEFAULT_CHUNKS)


def test_long_profile_outlasts_graceful_shutdown_and_stays_under_total_limit() -> None:
    long = MOCK_WORKLOAD_PROFILES["long"]
    stream_seconds = long.fragment_count * long.fragment_interval_seconds

    # 준비된 근거: uvicorn graceful 25초, terminationGracePeriod 30초, 전체 한도 180초
    assert 30 < stream_seconds < 180


def test_fragments_are_fixed_synthetic_text_only() -> None:
    for profile in MOCK_WORKLOAD_PROFILES.values():
        fragments = profile.fragments()

        assert len(fragments) == profile.fragment_count
        assert set(fragments) <= set(DEFAULT_CHUNKS)


def test_scaled_profile_emits_all_fragments_in_order_with_interval() -> None:
    # 준비: 실제 medium/long과 같은 규칙, 시간만 줄인 profile
    profile = MockWorkloadProfile("scaled-test", 5, 0.02)
    client = FakeInferenceClient.from_profile(profile)

    # 실행
    started = time.monotonic()
    emitted = list(client.start(uuid4(), [], max_tokens=512))
    elapsed = time.monotonic() - started

    # 검증: 조각마다 보낸 뒤 쉬므로 최소 fragment_count × interval
    assert emitted == list(profile.fragments())
    assert elapsed >= profile.fragment_count * profile.fragment_interval_seconds


def test_cancel_stops_profile_stream_without_exception() -> None:
    profile = MockWorkloadProfile("scaled-test", 50, 0.01)
    client = FakeInferenceClient.from_profile(profile)
    generation_id = uuid4()
    emitted = []

    for fragment in client.start(generation_id, [], max_tokens=512):
        emitted.append(fragment)
        if len(emitted) == 2:
            client.cancel(generation_id)

    assert len(emitted) < profile.fragment_count


@pytest.mark.parametrize(
    ("fragment_count", "interval"),
    [
        (0, 0.1),
        (1001, 0.0),
        (3, -0.1),
        (3, math.nan),
        (3, math.inf),
        (400, 0.5),  # 200초 — 전체 한도 180초를 넘는 스트림은 긴 정상 스트림이 아니다
    ],
)
def test_invalid_profile_values_fail_immediately(fragment_count: int, interval: float) -> None:
    with pytest.raises(ValueError):
        MockWorkloadProfile("bad", fragment_count, interval)


def test_blank_profile_name_fails() -> None:
    with pytest.raises(ValueError):
        MockWorkloadProfile("", 3, 0.0)
