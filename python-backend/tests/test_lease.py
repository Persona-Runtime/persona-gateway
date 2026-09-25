"""GenerationLeaseKeeper 단위 테스트(DB 없음).

규칙: heartbeat는 자기 행의 lease를 늘리고, cancel_requested인 행은 로컬 업스트림 취소로
전달한다(다른 인스턴스로 들어온 cancel). DB 장애는 메트릭으로 남기고 다음 주기에 재시도하며,
그 밖의 예외(코드 결함)는 숨기지 않는다.
"""

from __future__ import annotations

import time
from uuid import UUID, uuid4

import psycopg
import pytest
from prometheus_client import REGISTRY

from persona_minimal_api.chat.lease import GenerationLeaseKeeper


class FakeStore:
    def __init__(self, rows: list[tuple[UUID, str]] | None = None, error: Exception | None = None):
        self.rows = rows or []
        self.error = error
        self.calls = 0

    def extend_own_leases(self) -> list[tuple[UUID, str]]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.rows


class RecordingClient:
    def __init__(self) -> None:
        self.cancelled: list[UUID] = []

    def start(self, *args: object, **kwargs: object):  # pragma: no cover - 쓰지 않는다
        raise NotImplementedError

    def cancel(self, generation_id: UUID) -> None:
        self.cancelled.append(generation_id)


def failures() -> float:
    return REGISTRY.get_sample_value("persona_chat_lease_heartbeat_failures_total") or 0.0


def test_cancel_requested_rows_are_forwarded_to_local_upstream() -> None:
    running, cancel_requested = uuid4(), uuid4()
    client = RecordingClient()
    keeper = GenerationLeaseKeeper(
        FakeStore([(running, "running"), (cancel_requested, "cancel_requested")]),  # type: ignore[arg-type]
        client,
        heartbeat_seconds=1.0,
    )

    keeper.beat_once()

    assert client.cancelled == [cancel_requested]


def test_database_error_is_counted_and_does_not_raise() -> None:
    store = FakeStore(error=psycopg.OperationalError("synthetic db outage"))
    keeper = GenerationLeaseKeeper(store, RecordingClient(), heartbeat_seconds=1.0)  # type: ignore[arg-type]
    before = failures()

    keeper.beat_once()

    assert failures() == before + 1


def test_unexpected_error_is_not_hidden() -> None:
    store = FakeStore(error=RuntimeError("synthetic defect"))
    keeper = GenerationLeaseKeeper(store, RecordingClient(), heartbeat_seconds=1.0)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError):
        keeper.beat_once()


def test_heartbeat_thread_runs_until_stopped() -> None:
    store = FakeStore()
    keeper = GenerationLeaseKeeper(store, RecordingClient(), heartbeat_seconds=0.01)  # type: ignore[arg-type]

    keeper.start()
    time.sleep(0.1)
    keeper.stop()
    calls_at_stop = store.calls
    time.sleep(0.05)

    assert calls_at_stop >= 2
    assert store.calls == calls_at_stop
