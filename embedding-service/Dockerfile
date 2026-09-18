# python-backend/Dockerfile과 같은 패턴(uv·python 버전 고정, nonroot). DB가 없어
# migrations/alembic.ini는 없다.
FROM ghcr.io/astral-sh/uv:0.11.15@sha256:e590846f4776907b254ac0f44b5b380347af5d90d668138ca7938d1b0c2f98d3 AS uv

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

# 모델을 빌드 시점에 내려받아 이미지에 굽는다 — 런타임 네트워크 0을 위해서다.
# 이 RUN만 네트워크가 필요하고(HF_HUB_OFFLINE은 아직 안 걸려 있다), 그 뒤로는
# 오프라인이다. revision은 persona_embedding_service.config.MODEL_REVISION과
# 반드시 같아야 한다 — 여기서 그 상수를 그대로 읽어 쓴다.
RUN --mount=type=cache,target=/root/.cache/pip \
    HF_HUB_OFFLINE=0 .venv/bin/python -c \
    "from persona_embedding_service import config; from sentence_transformers import SentenceTransformer; \
     SentenceTransformer(config.MODEL_NAME, revision=config.MODEL_REVISION, device='cpu'); \
     print(f'baked model={config.MODEL_NAME} revision={config.MODEL_REVISION}')"

RUN chown -R persona:persona /app

USER 10001:10001
EXPOSE 8081

# exec-form entrypoint keeps Uvicorn as PID 1, so Kubernetes SIGTERM reaches it directly.
ENTRYPOINT ["/app/.venv/bin/uvicorn"]
CMD ["--factory", "persona_embedding_service.main:create_app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1", "--timeout-graceful-shutdown", "25"]
