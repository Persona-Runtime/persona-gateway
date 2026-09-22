# python-backend/Dockerfile과 같은 패턴(uv·python 버전 고정, nonroot). DB가 없어
# migrations/alembic.ini는 없다.
#
# 모델 "다운로드"(HF 허브에서 가중치 파일을 받는 것)와 "로딩"(torch로 텐서를 구성해
# 추론 가능한 상태로 만드는 것)을 서로 다른 스테이지로 나눈다. 예전엔
# `SentenceTransformer(...)` 한 호출로 이 둘을 한 번에 했는데, 그 RUN이 amd64
# 타깃 스테이지 안에 있어서 arm64 맥에서는 스테이지 전체가 QEMU 에뮬레이션으로
# 돌고, 에뮬레이션 위에서 torch import가 30분을 넘겨도 끝나지 않았다(2026-09-22
# 실측, 결국 빌드 중단) — amd64 빌드 안 torch import는 QEMU에서 데드락한다(30분+
# 기다려도 진행이 없다, 빌드뿐 아니라 --network none으로 완성된 amd64 이미지를
# 직접 실행해 봐도 같은 증상). 다운로드엔 torch가 필요 없으므로, model 스테이지는
# --platform=$BUILDPLATFORM로 네이티브 아키텍처(맥이면 arm64)에서 돌려 huggingface_hub
# 만으로 가중치를 받고, amd64 최종 스테이지는 그 결과(HF 표준 캐시 레이아웃)를 파일
# 그대로 복사만 한다 — torch/SentenceTransformer는 빌드 시점엔 전혀 등장하지 않고,
# 런타임에 main.py의 lifespan이 시작할 때만(백그라운드 스레드) 오프라인으로 로딩한다.
# 실측(2026-09-22, 맥 arm64, docker buildx build --platform linux/amd64 --load):
# 캐시 없이 처음부터 빌드하면 전체 6분 10초 — 그중 model 스테이지의 snapshot_download
# 자체는 2분 41초(HF 허브 다운로드 속도에 좌우되는 네트워크 대기이지 에뮬레이션이
# 아니다)이고 나머지는 이미지 export(2분 45초 — 이것도 아키텍처와 무관, 파일 크기
# 문제) 등이다. 핵심은 "5분 안"이 아니라 "끝난다"는 것 — 예전 방식은 같은 조건에서
# 30분을 넘겨도 안 끝나 결국 빌드를 중단했었다. 소스·설정이 안 바뀌어 캐시가 맞는
# 재빌드는 3초.
#
# 빌드캐시가 회당 수 GB다 — 실패한 빌드도 캐시에 그대로 남는다(이번에 amd64 +
# arm64 검증 빌드 두 번만으로 16GB가 쌓였다, 2026-09-22 실측). 로컬에서 이 Dockerfile로
# 반복 실험할 때는 끝날 때마다 `docker builder prune -f`로 비운다 — 안 그러면
# 디스크가 조용히 가득 찬다(이번 라운드에서 실제로 겪은 일).
FROM ghcr.io/astral-sh/uv:0.11.15@sha256:e590846f4776907b254ac0f44b5b380347af5d90d668138ca7938d1b0c2f98d3 AS uv

FROM --platform=$BUILDPLATFORM python:3.12-slim AS model

WORKDIR /build

# huggingface_hub는 pyproject.toml에 직접 선언돼 있지 않다(sentence-transformers의
# 간접 의존이라서) — 여기서만 쓰는 값이라 uv.lock에 고정된 버전(0.36.2)과 맞춰
# pip로 바로 설치한다. 런타임에 실제로 캐시를 읽는 huggingface_hub와 버전이
# 갈리면 캐시 레이아웃 해석이 달라질 여지가 생기므로 맞춰 둔다.
RUN pip install --no-cache-dir "huggingface_hub==0.36.2"

# MODEL_NAME·MODEL_REVISION은 config.py 한 곳에서만 정의한다(중복 상수 금지) —
# config.py는 os만 import해서 이 파일만 가져와도 torch·sentence-transformers가
# 전혀 끌려오지 않는다.
COPY src ./src
RUN python -c "\
import sys; sys.path.insert(0, 'src'); \
from persona_embedding_service import config; \
from huggingface_hub import snapshot_download; \
snapshot_download(repo_id=config.MODEL_NAME, revision=config.MODEL_REVISION, cache_dir='/hf-cache/hub'); \
print(f'downloaded model={config.MODEL_NAME} revision={config.MODEL_REVISION}')"

FROM docker.io/library/python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    HF_HOME=/app/hf-cache \
    HF_HUB_OFFLINE=1 \
    # 모델은 빌드 시 /app/hf-cache에 구워 넣고 런타임엔 읽기만 한다. 그래도
    # huggingface_hub/torch가 lock 파일 등 예상 밖의 쓰기를 시도할 수 있어
    # read-only rootfs에서 쓸 수 있는 유일한 경로(/tmp)로 캐시 후보를 몰아둔다.
    XDG_CACHE_HOME=/tmp \
    TORCH_HOME=/tmp/torch \
    TMPDIR=/tmp

COPY --from=uv /uv /uvx /bin/

RUN groupadd --gid 10001 persona && \
    useradd --uid 10001 --gid persona --create-home --shell /usr/sbin/nologin persona

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev

RUN chown -R persona:persona /app

# model 스테이지가 네이티브 아키텍처에서 내려받은 HF 캐시를 그대로 옮긴다. torch
# import·모델 로딩은 여기서 하지 않는다 — 런타임에 main.py의 lifespan이 시작할 때
# 백그라운드 스레드로 오프라인 로딩한다(model.py).
COPY --from=model --chown=10001:10001 /hf-cache /app/hf-cache

USER 10001:10001
EXPOSE 8081

# exec-form entrypoint keeps Uvicorn as PID 1, so Kubernetes SIGTERM reaches it directly.
ENTRYPOINT ["/app/.venv/bin/uvicorn"]
CMD ["--factory", "persona_embedding_service.main:create_app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1", "--timeout-graceful-shutdown", "25"]
