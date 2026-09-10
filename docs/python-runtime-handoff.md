# Python runtime 인수인계

## 이미지와 실행

```sh
docker buildx build --platform linux/amd64 --load \
  -t persona-minimal-api:local -f python-backend/Dockerfile python-backend
```

이미지는 Python 3.12.10 slim-bookworm(`sha256:fd95…588db`)과 uv 0.11.15
(`sha256:e590…2f98d`)의 immutable digest를 고정해 `uv.lock`을 검증 설치한다.
런타임은 UID/GID 10001의 비루트 사용자로 8080 포트에서 Uvicorn을 직접 실행한다.

필수 Secret 환경변수는 `DATABASE_URL`, `PERSONA_STATIC_BEARER_TOKEN`,
`PERSONA_STATIC_USER_ID`, `PERSONA_STATIC_DISPLAY_NAME`, `PERSONA_CURSOR_SIGNING_KEY`다.
선택값 `PERSONA_DB_TIMEOUT_SECONDS`의 기본값은 2초다.

## migration과 권한

앱 배포 전에 별도 migration 작업으로 실행한다. 앱 컨테이너 시작 명령에는 이 작업을 넣지 않는다.

```sh
docker run --rm --entrypoint /app/.venv/bin/alembic \
  --env DATABASE_URL=postgresql://... persona-minimal-api:local upgrade head
```

migrator는 schema 생성·DDL·Alembic version 변경 권한을, runtime은 앱 테이블의
`SELECT/INSERT/UPDATE`와 version 읽기 권한만 가진다. 두 역할의 DB 자격증명은 분리한다.

## Kubernetes probe와 종료

- liveness: `GET /healthz`, DB와 무관하게 200
- readiness: `GET /readyz`, DB와 필수 Alembic revision 확인; 실패 시 안전한 503
- readiness probe: `timeoutSeconds: 3`, `periodSeconds: 5`, `failureThreshold: 1`
- liveness probe: `timeoutSeconds: 1`, `periodSeconds: 10`, `failureThreshold: 3`
- `terminationGracePeriodSeconds: 30`; Uvicorn graceful shutdown은 25초

컨테이너는 read-only root filesystem으로 실행하고, 필요한 경우에만 `/tmp`를 `emptyDir`로
마운트한다. 실제 Secret, DB 주소, 예외 원문은 로그·응답·Git에 넣지 않는다.
