# persona-gateway

사용자 요청을 받는 Python 최소 백엔드와 API 계약을 소유한다. 현재 제공하는 API는
`GET /v1/me`, `GET /v1/personas`, `POST /v1/personas`, `GET /healthz`, `GET /readyz`다.
전체 목표 계약은 [읽기용 계약](api/service-api-v1.md)과 [OpenAPI](api/openapi.json)를 따른다.

## Python 최소 backend

`python-backend/`는 FastAPI·psycopg·Alembic 기반의 독립 런타임이다. Alembic만
`persona_minimal` PostgreSQL 스키마와 그 안의 `alembic_version`을 소유한다.
앱 시작 시 migration을 자동 실행하지 않는다.

로컬 실행:

```sh
cd python-backend
DATABASE_URL=postgresql://... uv run alembic upgrade head
uv run uvicorn --factory persona_minimal_api.main:create_app --host 127.0.0.1 --port 8080
```

컨테이너 build와 실행은 [Python runtime 인수인계](docs/python-runtime-handoff.md)를 따른다.
실제 토큰·DB 주소는 Git·로그·예제에 넣지 않는다.

## 범위와 보안

- Secret으로 `DATABASE_URL`, `PERSONA_STATIC_BEARER_TOKEN`, `PERSONA_STATIC_USER_ID`,
  `PERSONA_STATIC_DISPLAY_NAME`, `PERSONA_CURSOR_SIGNING_KEY`를 공급한다.
- `PERSONA_DB_TIMEOUT_SECONDS`는 선택값이며 기본 2초다. DB 연결, pool 대기와 readiness
  검사에 적용한다.
- `/healthz`는 DB 장애와 무관한 프로세스 생존 신호다. `/readyz`는 migration된 필수 스키마와
  DB 연결을 확인하며, 미준비 상태에는 민감한 세부 정보 없이 503을 반환한다.
- 업로드·ingestion·대화·삭제 등 나머지 API는 아직 구현하지 않았다. 미구현 기능을 동작하는
  것처럼 표시하지 않는다.

기존 Go Gateway·dispatcher·DDL은 활성 레포에서 제거하고, 검증된 로컬 보관본으로만 유지한다.
원격 push·PR·홈 클러스터 배포는 이 변경에 포함하지 않는다.
