"""별도 OS 프로세스로 띄우는 Gateway 앱 팩토리(G-1 SIGKILL 통합 테스트 전용).

`uvicorn --factory lease_subprocess_app:create`로 실행한다. 설정은 환경변수(Settings)에서
읽고, 업스트림만 느린 합성 mock으로 바꾼다 — 스트리밍 도중 프로세스를 SIGKILL해 "종료 코드가
실행되지 않은 장애(OOM·노드 장애)"를 재현하려는 목적이다. 테스트 파일이 아니라서 pytest가
수집하지 않는다(test_ 접두사 없음).
"""

from __future__ import annotations

from fastapi import FastAPI

from persona_minimal_api.chat.fake_inference import FakeInferenceClient
from persona_minimal_api.config import Settings
from persona_minimal_api.main import create_app

# 100조각 × 0.1초 ≈ 10초 — 테스트가 SIGKILL을 보내는 동안 스트림이 끝나지 않을 만큼 길다.
SLOW_FRAGMENT_COUNT = 100
SLOW_FRAGMENT_INTERVAL_SECONDS = 0.1


def create() -> FastAPI:
    return create_app(
        Settings(),
        inference_client=FakeInferenceClient(
            chunks=("합성 ",) * SLOW_FRAGMENT_COUNT,
            delay_between_chunks=SLOW_FRAGMENT_INTERVAL_SECONDS,
        ),
    )
