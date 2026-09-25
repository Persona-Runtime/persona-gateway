"""이 Gateway 인스턴스가 소유한 generation의 lease를 주기적으로 연장한다(G-1).

왜 필요한가: 롤링 배포에서 새 Pod는 이전 Pod와 겹쳐 뜬다. 새 Pod가 "살아 있는 소유자의 행"과
"죽은 소유자의 행"을 구분하려면, 살아 있는 인스턴스가 스스로 살아 있음을 DB에 계속 알려야
한다. 이 모듈의 heartbeat가 그 신호이고, 다른 인스턴스는 lease가 만료된 행만 회수한다
(chat/repository.py의 reclaim_* 참고). 설계와 미결 사항은 api/generation-ownership-lease-design.md.

부가 역할 — 다른 인스턴스로 들어온 cancel 전달: cancel 요청은 아무 Pod에나 도착할 수 있다.
그 Pod는 DB를 cancel_requested로 바꾸지만, 실제 스트림을 돌리는 Pod의 업스트림에는 신호가 닿지
않는다. heartbeat가 자기 행의 상태를 함께 읽어 cancel_requested를 보면 로컬 업스트림 취소를
부른다 — 지연은 heartbeat 간격 이하다. 로컬 cancel은 멱등이라 같은 행을 여러 번 봐도 안전하다.

실패 시 동작: 연장 실패(DB 장애 등)는 메트릭·로그(예외 타입만)로 남기고 다음 주기에 다시
시도한다. 계속 실패하면 lease가 만료되고 다른 인스턴스가 회수할 수 있다 — 소유권을 증명하지
못하는 인스턴스의 행을 남이 가져가는 쪽이 의도된 안전 방향이다. 그 경우에도 이 인스턴스의
finish_generation은 reconciling 행을 덮어쓰지 않는다(기존 WHERE 가드).
"""

from __future__ import annotations

import logging
import threading

from psycopg import Error as PsycopgError
from psycopg_pool import PoolTimeout

from . import metrics
from .inference import InferenceClient
from .repository import ChatStore

logger = logging.getLogger(__name__)

# stop()이 heartbeat 스레드 종료를 기다리는 최대 시간(초). 한 번의 연장 문장은 DB timeout
# (기본 2초) 안에 끝나므로 그보다 넉넉하게 둔다. 초과해도 스레드는 daemon이라 종료를 막지 않는다.
STOP_JOIN_TIMEOUT_SECONDS = 5.0


class GenerationLeaseKeeper:
    """heartbeat 스레드 하나를 소유한다. start()·stop()은 앱 lifespan이 한 번씩 부른다."""

    def __init__(
        self,
        chat_store: ChatStore,
        inference_client: InferenceClient,
        *,
        heartbeat_seconds: float,
    ) -> None:
        self._chat_store = chat_store
        self._inference_client = inference_client
        self._heartbeat_seconds = heartbeat_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="generation-lease-heartbeat", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """heartbeat를 멈춘다. 멈춘 뒤에는 lease가 늘지 않으므로, 호출자는 이어서 자기 행을
        반납(release_own_generations)하거나 — 비정상 종료처럼 — 만료에 맡긴다."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=STOP_JOIN_TIMEOUT_SECONDS)

    def beat_once(self) -> None:
        """한 번 연장한다. 스레드 루프와 테스트가 함께 쓴다."""
        try:
            owned = self._chat_store.extend_own_leases()
        except (PsycopgError, PoolTimeout) as error:
            # DB 장애만 여기서 삼킨다 — 다음 주기에 재시도해야 하고, 스레드가 죽으면 이
            # 인스턴스의 모든 행이 만료돼 버린다. 그 밖의 예외(코드 결함)는 숨기지 않는다.
            # 로그에는 예외 타입만 남긴다 — 메시지에 SQL·접속 정보가 섞일 수 있다.
            metrics.LEASE_HEARTBEAT_FAILURES.inc()
            logger.warning("generation lease 연장 실패(%s)", type(error).__name__)
            return
        for generation_id, status in owned:
            if status == "cancel_requested":
                self._inference_client.cancel(generation_id)

    def _run(self) -> None:
        # 첫 연장은 한 간격 뒤에 한다 — 행을 만들 때(INSERT·running 전환) 이미 lease를 채운다.
        while not self._stop.wait(self._heartbeat_seconds):
            self.beat_once()
