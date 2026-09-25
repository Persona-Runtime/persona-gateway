# Python과 uv 버전을 고정해 lockfile 해석과 런타임 ABI가 바뀌지 않게 한다.
FROM ghcr.io/astral-sh/uv:0.11.15@sha256:e590846f4776907b254ac0f44b5b380347af5d90d668138ca7938d1b0c2f98d3 AS uv

FROM docker.io/library/python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:$PATH

COPY --from=uv /uv /uvx /bin/

RUN groupadd --gid 10001 persona && \
    useradd --uid 10001 --gid persona --create-home --shell /usr/sbin/nologin persona

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --locked --no-dev && \
    chown -R persona:persona /app

# 빌드한 Git commit을 persona_gateway_build_info의 revision label로 노출한다. 롤아웃 중
# 어느 버전이 트래픽을 받는지 요청 지표와 겹쳐 보기 위해서다. 레이어 캐시를 덜 깨려고
# 소스 복사 뒤에 둔다. 인자를 주지 않거나 hash 형식이 아니면 앱이 "unknown"으로 기록한다.
ARG GIT_REVISION=unknown
ENV PERSONA_BUILD_REVISION=$GIT_REVISION

USER 10001:10001
EXPOSE 8080

# exec-form entrypoint keeps Uvicorn as PID 1, so Kubernetes SIGTERM reaches it directly.
ENTRYPOINT ["/app/.venv/bin/uvicorn"]
CMD ["--factory", "persona_minimal_api.main:create_app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "25"]
