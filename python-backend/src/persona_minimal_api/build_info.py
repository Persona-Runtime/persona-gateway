"""실행 중인 Gateway의 버전·Git revision을 Prometheus info 메트릭으로 알린다.

롤링 배포 중 어느 시점에 새 버전 Pod가 트래픽을 받기 시작했는지를 요청 지표와 겹쳐 보기
위한 것이다(`count by (revision) (persona_gateway_build_info)`). 프로세스 시작 시각은
prometheus_client 기본 ProcessCollector의 `process_start_time_seconds`를 그대로 쓴다.

label 값은 기동 시 한 번만 정해진다 — 요청마다 새 label 값이 생기지 않는다.
"""

from __future__ import annotations

import os
import re
from importlib.metadata import PackageNotFoundError, version

from prometheus_client import Info

PACKAGE_NAME = "persona-minimal-api"
REVISION_ENV = "PERSONA_BUILD_REVISION"
UNKNOWN = "unknown"
# Git commit hash(짧은 7자~전체 40자)만 받는다. 이미지 빌드 인자에 실수로 브랜치 이름이나
# 임의 문자열이 들어와도 그대로 label이 되지 않게 한다.
REVISION_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")

BUILD_INFO = Info("persona_gateway_build", "실행 중인 Gateway의 패키지 버전과 Git revision")


def package_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        # 설치하지 않고 소스 경로로만 실행한 경우. 추측한 값을 넣지 않는다.
        return UNKNOWN


def normalized_revision(raw: str | None) -> str:
    """env 값이 Git hash 형식이면 그대로, 아니면 `unknown`을 돌려준다."""
    if raw is None:
        return UNKNOWN
    candidate = raw.strip().lower()
    return candidate if REVISION_PATTERN.fullmatch(candidate) else UNKNOWN


def record_build_info() -> None:
    """기동 시 한 번 부른다. 다시 불러도 같은 값으로 덮어쓸 뿐 series가 늘지 않는다."""
    BUILD_INFO.info(
        {
            "version": package_version(),
            "revision": normalized_revision(os.environ.get(REVISION_ENV)),
        }
    )
