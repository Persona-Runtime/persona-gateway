# 컨테이너 검증 기록 — 34d65f5

`scripts/smoke-container.sh`로 실행한 검증 결과다. 이 문서는 **로컬 검증까지의 기록**이며,
GHCR push와 홈 클러스터 배포는 포함하지 않는다.

## 대상

| 항목 | 값 |
| --- | --- |
| 소스 커밋 | `34d65f5` (`fix: sanitize pool logs across all levels`) |
| 브랜치 | `chore/api-job` |
| 이미지 태그 | `persona-minimal-api:34d65f5` |
| 이미지 ID | `sha256:4b967ab8aff1dde28014bd6e0f7a097bef6acf6649f0a730859330c8e48bc185` |
| 아키텍처 | `linux/amd64` |
| 실행 사용자 | UID/GID `10001:10001` |
| entrypoint | `/app/.venv/bin/uvicorn` |
| 포트 | 8080 |

이미지는 `python-backend/Dockerfile`의 고정 digest(Python 3.12.10 slim-bookworm, uv 0.11.15)와
`uv.lock`으로 빌드했다. 태그는 소스 커밋 SHA를 그대로 쓴다.

## 실행 조건

검증 실행일 기준 호스트는 **arm64 macOS이며 Docker가 linux/amd64를 에뮬레이션**했다.
기능 검증으로만 쓰고 **성능 기준선으로 사용하지 않는다.** 실제 amd64 하드웨어 실행은 미검증이다.

API 컨테이너는 배포 설정과 같은 제약으로 실행했다.

```
--user 10001:10001 --read-only --cap-drop ALL --security-opt no-new-privileges
--tmpfs /tmp:rw,nosuid,nodev,noexec,uid=10001,gid=10001,mode=1700
```

`PYTHONDONTWRITEBYTECODE=1`이라 read-only rootfs에서 추가 쓰기 경로가 필요 없었다.
`/tmp`만 `emptyDir`로 주면 된다.

DB는 이 실행에서만 쓰는 임시 Postgres 컨테이너이고 **호스트 포트를 공개하지 않았다.**
사용자·토큰·캐릭터 이름은 모두 합성 값이다. 기존 `mafest-postgres`, `persona-web-e2e-pg`는
재사용하거나 변경하지 않았다.

migration은 앱 시작과 분리해 같은 이미지의 entrypoint만 바꿔 실행했다.

```
docker run --rm --entrypoint /app/.venv/bin/alembic --env DATABASE_URL=... <image> upgrade head
```

적용된 revision은 `0001_persona_minimal`이다.

## 검증 결과

`PERSONA_DB_TIMEOUT_SECONDS=2`, `docker stop --time 30` 기준.

**측정 방법과 해상도** — 잠금 시 readiness 소요 시간은 curl의 `%{time_total}`로 잰다.
재려는 값이 바로 그 요청의 응답 시간이기 때문이다. 종료 시간은 `/usr/bin/perl -MTime::HiRes`로
재며 해상도는 1ms다. `date +%s`는 정수 초라 1초 미만이 전부 "0초"로 보인다. 이 스크립트가 도는
bash 3.2에는 `$EPOCHREALTIME`이 없고 macOS의 `date`는 `%N`을 지원하지 않는다.
아래 수치는 **한 번의 실행에서 관측된 값**이며 상한 보장이 아니다.

잠금 시 2.016914초는 앱에 준 예산 2초(`PERSONA_DB_TIMEOUT_SECONDS`)의 `statement_timeout`이
잠금 대기를 끊은 결과다. 검증 상한 6초와는 역할이 다르다 — 상한은 판정 기준이고, 예산은
검증 대상이다. 요청 자체에는 별도의 안전 상한(연결 3초·전체 10초)이 걸려 있어 서버가 응답하지
않아도 검증이 멈추지 않는다.

| 시나리오 | 결과 |
| --- | --- |
| 정상 기동 | 통과 — `/healthz` 200, `/readyz` 200 |
| 잘못된 토큰 | 통과 — 401, 오류 응답에 기대 토큰 미노출 |
| 정상 인증 | 통과 — `/v1/me`가 합성 사용자 반환 |
| 캐릭터 생성·조회 | 통과 — 201 생성, 목록에 반영 |
| 같은 키·같은 요청 재전송 | 통과 — 같은 ID 반환, `personas` 행 수 1 유지 |
| API 컨테이너 재시작 | 통과 — 기존 캐릭터 유지 |
| DB 테이블 잠금 | 통과 — `alembic_version` 배타 잠금 상태에서 **2.016914초** 만에 503, 같은 시점 `/healthz`는 200 |
| DB 중단 | 통과 — `/healthz` 200 유지, `/readyz`·`/v1/personas` 503 |
| DB 복구 | 통과 — readiness·목록 조회 정상 복귀 |
| SIGTERM | 통과 — **0.388초** 만에 exit 0 (유예 30초, Uvicorn graceful 25초) |

### 로그 비노출

두 가지를 나눠 확인했다. 원문 로그는 기록하지 않고 통과 여부만 남긴다.

1. **이미지 안에 설치된 필터 코드** — 이미지의 `persona_minimal_api.repository.configure_pool_logging()`을
   직접 실행해 검사했다. `psycopg.pool` 로거를 DEBUG로 낮추고 합성 주소·사용자명·비밀번호를 넣어도
   DEBUG·INFO는 **출력 자체가 없었고**, WARNING·ERROR·CRITICAL은 예외 traceback 없이
   고정 문구 `database pool connection unavailable`만 남았다. 통과.
2. **실제 DB 장애 로그** — 위 DB 중단·복구 시나리오로 실제 pool 오류를 발생시킨 뒤 컨테이너 로그에
   합성 DB 비밀번호·사용자명·호스트명·bearer 토큰·cursor 서명 키가 있는지 검사했다. 미노출. 통과.

## 미검증 항목

- 실제 linux/amd64 하드웨어에서의 실행 (이번은 에뮬레이션)
- 성능·지연 수치 — 에뮬레이션이라 측정하지 않았다
- GHCR registry digest — 아직 push하지 않았다. **위 이미지 ID를 registry digest로 쓰지 않는다.**
- Kubernetes 환경에서의 probe 동작, migration Job 실패 시 배포 중단 절차
- migrator/runtime DB 권한 분리 — 이번 smoke는 단일 역할로 실행했다
- 동시 요청·부하 상황에서의 캐릭터 한도(3개) 경합

## 재실행

```sh
scripts/smoke-container.sh persona-minimal-api:34d65f5
```

인자를 생략하면 위 태그를 기본값으로 쓴다. 스크립트는 전용 network·Postgres를 새로 만들고
끝나면 자기가 만든 자원만 제거한다. 임시 컨테이너·network와 함께 **Postgres의 익명 데이터
볼륨까지** 제거한다(`docker rm --force --volumes`). 다른 작업의 볼륨을 건드리는 `volume prune`은
쓰지 않는다. 실행 전후 dangling 볼륨 수가 14 → 14로 같음을 확인했다.

다른 태그나 GHCR digest로 다시 검증할 때도 같은 스크립트를 쓴다.
스크립트는 `docker`·`curl`·`grep`·`awk`·`perl`·`mktemp`가 없으면 검사를 조용히 건너뛰지 않고 멈춘다.
