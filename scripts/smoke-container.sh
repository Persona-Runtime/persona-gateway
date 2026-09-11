#!/usr/bin/env bash
set -euo pipefail

# 빌드한 컨테이너 이미지가 실제로 동작하는지 확인한다. 단위 테스트는 소스를 검사하지만
# 이 스크립트는 "이미지 안에 설치된 코드"와 비루트·read-only 실행 조건까지 함께 본다.
#
# 사용법:  scripts/smoke-container.sh [image]
# 기본값:  persona-minimal-api:34d65f5
#
# 이 스크립트는 전용 임시 network와 Postgres를 새로 만들고, 끝나면 자기가 만든 것만 지운다.
# 기존 mafest-postgres, persona-web-e2e-pg 등 다른 작업의 컨테이너는 건드리지 않는다.
# 실제 사용자 자료는 쓰지 않는다. 아래 값은 모두 이 실행에서만 쓰는 합성 값이다.

image="${1:-persona-minimal-api:34d65f5}"

# PID를 붙여 이름이 겹치지 않게 한다. 다른 컨테이너를 실수로 재사용하지 않기 위함이다.
prefix="gw-smoke-$$"
network="${prefix}-net"
postgres="${prefix}-pg"
api="${prefix}-api"

# --- 합성 자격증명 (실제 값 아님) ---
db_user="smoke_user"
db_password="smoke_password_not_real"
db_name="smoke_persona"
database_url="postgresql://${db_user}:${db_password}@${postgres}:5432/${db_name}"
bearer_token="smoke-bearer-token-not-real"
user_id="smoke-user-0001"
display_name="스모크 사용자"
cursor_key="smoke-cursor-signing-key-not-real"

# readiness 예산. config.py 기본값과 같은 2초를 명시적으로 준다.
db_timeout_seconds="2"
# readiness가 잠금·장애에서 돌아와야 하는 상한. 예산 2초 + probe 여유.
readiness_deadline_seconds="6"
# Deployment에 넣을 terminationGracePeriodSeconds와 같은 값으로 종료를 확인한다.
termination_grace_seconds="30"

workdir="$(mktemp -d)"
failures=0

cleanup() {
  # 이번 실행이 만든 자원만 이름으로 지정해 제거한다.
  docker rm --force "$api" "$postgres" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  rm -rf "$workdir"
}
trap cleanup EXIT

ok()   { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1" >&2; failures=$((failures + 1)); }
step() { printf '\n[%s]\n' "$1"; }

# 상태 코드만 비교한다. 응답 본문은 민감할 수 있으므로 기본적으로 출력하지 않는다.
status_of() {
  curl --silent --output "$workdir/body.json" --write-out '%{http_code}' "$@"
}

expect_status() {
  local want="$1" desc="$2"
  shift 2
  local got
  got="$(status_of "$@")"
  if [ "$got" = "$want" ]; then
    ok "$desc (${got})"
  else
    fail "$desc: ${want} 기대, ${got} 수신"
  fi
}

# psql은 Postgres 컨테이너 안에서만 실행한다. DB 포트를 호스트에 공개하지 않기 위함이다.
psql_exec() {
  docker exec --env PGPASSWORD="$db_password" "$postgres" \
    psql --quiet --no-align --tuples-only --username "$db_user" --dbname "$db_name" "$@"
}

step "0. 환경 정보"
echo "  host arch    : $(uname -m)"
echo "  docker server: $(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')"
echo "  image        : ${image}"
image_platform="$(docker image inspect "$image" --format '{{.Os}}/{{.Architecture}}')"
image_id="$(docker image inspect "$image" --format '{{.Id}}')"
image_user="$(docker image inspect "$image" --format '{{.Config.User}}')"
image_entrypoint="$(docker image inspect "$image" --format '{{json .Config.Entrypoint}}')"
echo "  platform     : ${image_platform}"
echo "  image id     : ${image_id}"
echo "  user         : ${image_user}"
echo "  entrypoint   : ${image_entrypoint}"
if [ "$image_platform" != "linux/amd64" ]; then
  fail "이미지가 linux/amd64가 아님: ${image_platform}"
fi
if [ "$image_user" != "10001:10001" ]; then
  fail "이미지 기본 사용자가 10001:10001이 아님: ${image_user}"
fi
if [ "$(uname -m)" != "x86_64" ]; then
  echo "  주의: 현재 호스트는 amd64가 아니므로 에뮬레이션 실행이다. 기능 검증으로만 쓰고"
  echo "        성능 기준선으로 사용하지 않는다."
fi

step "1. 격리된 smoke 환경 준비"
docker network create "$network" >/dev/null
ok "임시 network 생성: ${network}"

# DB는 network 안에서만 접근한다. 호스트 포트를 공개하지 않아 다른 작업과 섞이지 않는다.
docker run --detach --name "$postgres" --network "$network" \
  --env POSTGRES_USER="$db_user" \
  --env POSTGRES_PASSWORD="$db_password" \
  --env POSTGRES_DB="$db_name" \
  postgres:16-alpine >/dev/null
ok "임시 Postgres 기동 (호스트 포트 비공개)"

for _ in $(seq 1 30); do
  if docker exec "$postgres" pg_isready --username "$db_user" --dbname "$db_name" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$postgres" pg_isready --username "$db_user" --dbname "$db_name" >/dev/null
ok "Postgres 접속 준비 완료"

step "2. 같은 이미지로 별도 migration 실행"
# 앱 시작 명령에 migration을 넣지 않는다는 계약을 그대로 따른다. entrypoint만 바꿔 실행한다.
docker run --rm --network "$network" --platform linux/amd64 \
  --entrypoint /app/.venv/bin/alembic \
  --env DATABASE_URL="$database_url" \
  "$image" upgrade head >"$workdir/migrate.log" 2>&1 || {
    echo "--- migration 실패 로그 ---" >&2
    cat "$workdir/migrate.log" >&2
    exit 1
  }
ok "alembic upgrade head 완료"

applied_revision="$(psql_exec --command 'SELECT version_num FROM persona_minimal.alembic_version' | tr -d '[:space:]')"
if [ "$applied_revision" = "0001_persona_minimal" ]; then
  ok "alembic revision 확인: ${applied_revision}"
else
  fail "alembic revision이 예상과 다름: ${applied_revision}"
fi

step "3. API 컨테이너 기동 (비루트 · read-only · 권한 최소화)"
docker run --detach --name "$api" --network "$network" --platform linux/amd64 \
  --user 10001:10001 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,uid=10001,gid=10001,mode=1700 \
  --publish 127.0.0.1::8080 \
  --env DATABASE_URL="$database_url" \
  --env PERSONA_STATIC_BEARER_TOKEN="$bearer_token" \
  --env PERSONA_STATIC_USER_ID="$user_id" \
  --env PERSONA_STATIC_DISPLAY_NAME="$display_name" \
  --env PERSONA_CURSOR_SIGNING_KEY="$cursor_key" \
  --env PERSONA_DB_TIMEOUT_SECONDS="$db_timeout_seconds" \
  "$image" >/dev/null

port="$(docker port "$api" 8080/tcp | awk -F: 'NR == 1 { print $NF }')"
base_url="http://127.0.0.1:${port}"
ok "컨테이너 기동, 호스트 노출: 127.0.0.1:${port}"

running_uid="$(docker exec "$api" id -u)"
if [ "$running_uid" = "10001" ]; then
  ok "실행 UID 10001 확인"
else
  fail "실행 UID가 10001이 아님: ${running_uid}"
fi

step "4. 시나리오 검증"

# --- 정상 기동 ---
ready=false
for _ in $(seq 1 30); do
  if [ "$(status_of "${base_url}/readyz")" = "200" ]; then
    ready=true
    break
  fi
  sleep 1
done
if [ "$ready" = true ]; then
  ok "정상 기동: /readyz 200"
else
  fail "정상 기동: /readyz가 준비되지 않음"
fi
expect_status 200 "정상 기동: /healthz 200" "${base_url}/healthz"

# --- 잘못된 토큰 ---
expect_status 401 "잘못된 토큰: 401" \
  --header "Authorization: Bearer wrong-token-not-real" "${base_url}/v1/me"
# 401 본문에 기대한 토큰이 그대로 실려 나가면 안 된다.
if grep -q "$bearer_token" "$workdir/body.json"; then
  fail "잘못된 토큰: 오류 응답에 기대 토큰이 노출됨"
else
  ok "잘못된 토큰: 오류 응답에 민감 정보 없음"
fi

# --- 정상 인증 ---
me_status="$(status_of --header "Authorization: Bearer ${bearer_token}" "${base_url}/v1/me")"
if [ "$me_status" = "200" ] && grep -q "$user_id" "$workdir/body.json"; then
  ok "정상 인증: /v1/me가 합성 사용자 반환"
else
  fail "정상 인증: /v1/me 실패 (status ${me_status})"
fi

# --- 캐릭터 생성·조회 ---
idem_key="11111111-2222-4333-8444-555555555555"
persona_name="스모크 캐릭터"
create_status="$(status_of --request POST \
  --header "Authorization: Bearer ${bearer_token}" \
  --header "Idempotency-Key: ${idem_key}" \
  --header "Content-Type: application/json" \
  --data "{\"name\":\"${persona_name}\"}" \
  "${base_url}/v1/personas")"
created_id="$(sed -n 's/.*"id":"\([^"]*\)".*/\1/p' "$workdir/body.json")"
if [ "$create_status" = "201" ] && [ -n "$created_id" ]; then
  ok "캐릭터 생성: 201, id=${created_id}"
else
  fail "캐릭터 생성 실패 (status ${create_status})"
fi

list_status="$(status_of --header "Authorization: Bearer ${bearer_token}" "${base_url}/v1/personas")"
if [ "$list_status" = "200" ] && grep -q "$created_id" "$workdir/body.json"; then
  ok "캐릭터 조회: 목록에 생성 결과 반영"
else
  fail "캐릭터 조회 실패 (status ${list_status})"
fi

# --- 같은 키·같은 요청 재전송 ---
replay_status="$(status_of --request POST \
  --header "Authorization: Bearer ${bearer_token}" \
  --header "Idempotency-Key: ${idem_key}" \
  --header "Content-Type: application/json" \
  --data "{\"name\":\"${persona_name}\"}" \
  "${base_url}/v1/personas")"
replay_id="$(sed -n 's/.*"id":"\([^"]*\)".*/\1/p' "$workdir/body.json")"
persona_rows="$(psql_exec --command 'SELECT count(*) FROM persona_minimal.personas' | tr -d '[:space:]')"
if [ "$replay_status" = "201" ] && [ "$replay_id" = "$created_id" ] && [ "$persona_rows" = "1" ]; then
  ok "재전송: 같은 ID 반환, 중복 생성 없음 (rows=${persona_rows})"
else
  fail "재전송: status=${replay_status} id=${replay_id} rows=${persona_rows}"
fi

# --- API 컨테이너 재시작 ---
docker restart "$api" >/dev/null
# 동적 포트 공개(127.0.0.1::8080)는 재시작 때 호스트 포트가 새로 배정된다. 다시 읽지 않으면
# 이어지는 요청이 사라진 포트로 가서 실제 동작과 무관하게 실패한다.
port="$(docker port "$api" 8080/tcp | awk -F: 'NR == 1 { print $NF }')"
base_url="http://127.0.0.1:${port}"
restarted=false
for _ in $(seq 1 30); do
  if [ "$(status_of "${base_url}/readyz")" = "200" ]; then
    restarted=true
    break
  fi
  sleep 1
done
if [ "$restarted" = true ]; then
  list_status="$(status_of --header "Authorization: Bearer ${bearer_token}" "${base_url}/v1/personas")"
  if [ "$list_status" = "200" ] && grep -q "$created_id" "$workdir/body.json"; then
    ok "API 재시작: 기존 캐릭터 유지 (호스트 포트 ${port})"
  else
    fail "API 재시작: 목록 복구 실패 (status ${list_status})"
  fi
else
  fail "API 재시작: readiness 복귀 실패"
fi

# --- DB 테이블 잠금 ---
# readiness는 alembic_version을 읽는다. 그 테이블을 배타 잠금해 probe가 DB에서 대기하게 만든다.
# probe timeout은 실행 중인 동기 SQL을 취소하지 못하므로, statement_timeout이 잠금 대기를
# 끊어 제한 시간 안에 503이 나오는지가 이 검사의 핵심이다.
docker exec --detach --env PGPASSWORD="$db_password" "$postgres" \
  psql --username "$db_user" --dbname "$db_name" --command \
  "BEGIN; LOCK TABLE persona_minimal.alembic_version IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(20); COMMIT;"
sleep 2
lock_start="$(date +%s)"
lock_status="$(status_of "${base_url}/readyz")"
lock_elapsed="$(( $(date +%s) - lock_start ))"
if [ "$lock_status" = "503" ] && [ "$lock_elapsed" -le "$readiness_deadline_seconds" ]; then
  ok "테이블 잠금: ${lock_elapsed}초 만에 503 (상한 ${readiness_deadline_seconds}초)"
else
  fail "테이블 잠금: status=${lock_status}, ${lock_elapsed}초 소요"
fi
expect_status 200 "테이블 잠금 중에도 /healthz 200" "${base_url}/healthz"
# 잠금 트랜잭션을 즉시 끊어 다음 시나리오에 영향을 주지 않게 한다.
psql_exec --command \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
   WHERE query LIKE '%ACCESS EXCLUSIVE%' AND pid <> pg_backend_pid()" >/dev/null
sleep 1

# --- DB 중단 ---
docker stop "$postgres" >/dev/null
expect_status 200 "DB 중단: /healthz 200 유지" "${base_url}/healthz"
down_ok=false
for _ in $(seq 1 15); do
  if [ "$(status_of "${base_url}/readyz")" = "503" ]; then
    down_ok=true
    break
  fi
  sleep 1
done
if [ "$down_ok" = true ]; then
  ok "DB 중단: /readyz 503"
else
  fail "DB 중단: /readyz가 503으로 바뀌지 않음"
fi
dep_status="$(status_of --header "Authorization: Bearer ${bearer_token}" "${base_url}/v1/personas")"
if [ "$dep_status" = "503" ]; then
  ok "DB 중단: DB 의존 API가 안전한 503"
else
  fail "DB 중단: /v1/personas가 ${dep_status} 반환"
fi

# --- DB 복구 ---
docker start "$postgres" >/dev/null
recovered=false
for _ in $(seq 1 60); do
  if [ "$(status_of "${base_url}/readyz")" = "200" ]; then
    recovered=true
    break
  fi
  sleep 1
done
if [ "$recovered" = true ]; then
  list_status="$(status_of --header "Authorization: Bearer ${bearer_token}" "${base_url}/v1/personas")"
  if [ "$list_status" = "200" ] && grep -q "$created_id" "$workdir/body.json"; then
    ok "DB 복구: readiness·목록 조회 정상 복귀"
  else
    fail "DB 복구: 목록 조회 실패 (status ${list_status})"
  fi
else
  fail "DB 복구: readiness 복귀 실패"
fi

step "5. 로그 비노출 검증"

# 5-1. 이미지 안에 설치된 필터 코드를 그대로 실행한다. 소스 트리가 아니라 이미지가 대상이다.
# --interactive가 있어야 heredoc이 컨테이너의 stdin으로 전달된다.
docker run --rm --interactive --platform linux/amd64 --entrypoint /app/.venv/bin/python "$image" - <<'PY' >"$workdir/logfilter.out" 2>&1
import io
import logging

from persona_minimal_api.repository import configure_pool_logging

SECRET_SAMPLES = ["smoke_password_not_real", "smoke-db-host.internal", "smoke_user"]

configure_pool_logging()

stream = io.StringIO()
handler = logging.StreamHandler(stream)
handler.setLevel(logging.DEBUG)
logger = logging.getLogger("psycopg.pool")
logger.setLevel(logging.DEBUG)
logger.propagate = False
logger.addHandler(handler)

# DEBUG/INFO는 통째로 버려야 한다. 진단용으로 레벨을 낮춰도 원문이 남으면 안 되기 때문이다.
for level in (logging.DEBUG, logging.INFO):
    logger.log(level, "connection to %s failed for user %s: %s", *SECRET_SAMPLES)
low_level_output = stream.getvalue()

# WARNING 이상은 고정 안전 문구만 남아야 한다.
try:
    raise RuntimeError("password authentication failed for user smoke_user")
except RuntimeError:
    logger.warning("pool failed: %s", SECRET_SAMPLES[0], exc_info=True)
logger.error("pool failed for %s", SECRET_SAMPLES[1])
logger.critical("pool failed for %s", SECRET_SAMPLES[2])
high_level_output = stream.getvalue()[len(low_level_output):]

problems = []
if low_level_output:
    problems.append("DEBUG/INFO 로그가 출력됨")
for sample in SECRET_SAMPLES:
    if sample in high_level_output:
        problems.append("WARNING 이상 로그에 민감 값이 남음")
        break
if "Traceback" in high_level_output:
    problems.append("WARNING 이상 로그에 예외 원문이 남음")
expected = "database pool connection unavailable"
emitted = [line for line in high_level_output.splitlines() if line.strip()]
if len(emitted) != 3 or any(line.strip() != expected for line in emitted):
    problems.append("WARNING 이상 로그가 고정 안전 문구가 아님")

# 원문 로그는 출력하지 않는다. 통과 여부만 남긴다.
print("LOGFILTER_FAIL: " + "; ".join(problems) if problems else "LOGFILTER_OK")
PY
if grep -q '^LOGFILTER_OK$' "$workdir/logfilter.out"; then
  ok "이미지 내 필터: DEBUG·INFO 미출력, WARNING 이상 고정 문구만"
else
  fail "이미지 내 필터: $(grep '^LOGFILTER_FAIL' "$workdir/logfilter.out" || echo '실행 실패')"
fi

# 5-2. 위 DB 장애 시나리오에서 실제로 쌓인 컨테이너 로그에 민감한 값이 없는지 본다.
docker logs "$api" >"$workdir/api.log" 2>&1
leaked=""
for sample in "$db_password" "$db_user" "$postgres" "$bearer_token" "$cursor_key"; do
  if grep -qF "$sample" "$workdir/api.log"; then
    leaked="yes"
  fi
done
if [ -z "$leaked" ]; then
  ok "실제 DB 장애 로그: 자격증명·호스트·토큰 미노출"
else
  # 어떤 값이 샜는지도 로그에 남기지 않는다. 사람이 직접 확인해야 한다.
  fail "실제 DB 장애 로그에 민감한 값이 포함됨 (원문은 출력하지 않음)"
fi

step "6. SIGTERM 종료"
stop_start="$(date +%s)"
docker stop --time "$termination_grace_seconds" "$api" >/dev/null
stop_elapsed="$(( $(date +%s) - stop_start ))"
exit_code="$(docker inspect "$api" --format '{{.State.ExitCode}}')"
if [ "$stop_elapsed" -lt "$termination_grace_seconds" ] && [ "$exit_code" = "0" ]; then
  ok "SIGTERM: ${stop_elapsed}초 만에 정상 종료 (exit ${exit_code}, 유예 ${termination_grace_seconds}초)"
else
  fail "SIGTERM: ${stop_elapsed}초 소요, exit ${exit_code}"
fi

step "결과"
if [ "$failures" -eq 0 ]; then
  echo "  smoke 통과: ${image} (${image_platform}, id ${image_id})"
  exit 0
fi
echo "  실패 ${failures}건" >&2
exit 1
