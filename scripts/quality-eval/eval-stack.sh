#!/usr/bin/env bash
set -eu

# 골든셋 전용 격리 스택: Postgres(컨테이너) + embedding-service + Gateway(호스트 프로세스).
#
# scripts/local-stack.sh와 목적이 다르다. 그쪽은 이미지·DB 권한 분리를 검증하고 임베딩 서비스를
# 띄우지 않는다(apply가 실패한다). 여기는 "같은 질문을 다른 코드·설정으로 다시 묻기"가 목적이라
# - 임베딩 서비스를 실제로 띄우고(색인이 돼야 검색 지표를 잴 수 있다),
# - Gateway를 **소스 디렉터리에서 직접** 띄운다. baseline(develop)과 변경 브랜치를
#   --gateway-src만 바꿔 같은 DB·같은 임베딩으로 비교하기 위해서다.
# DB는 superuser 하나로 migration과 실행을 함께 한다(권한 분리 검증은 local-stack.sh의 책임).
#
# 사용법:
#   scripts/quality-eval/eval-stack.sh up [--gateway-src DIR]
#   scripts/quality-eval/eval-stack.sh restart-gateway [--gateway-src DIR]   # 설정·코드만 바꿔 재기동
#   scripts/quality-eval/eval-stack.sh down
#
# Gateway 설정은 호출한 셸의 환경변수를 그대로 물려받는다. 예:
#   PERSONA_CHAT_INFERENCE_MODE=llm PERSONA_VLLM_BASE_URL=http://<격리 vLLM> \
#   PERSONA_VLLM_MODEL=<served-model-name> PERSONA_PROMPT_VERSION=v2 \
#   scripts/quality-eval/eval-stack.sh up
# 운영 vLLM·운영 DB 주소를 넣지 않는다. 아래 자격증명은 전부 이 실행 전용 합성 값이다.

for tool in docker curl uv; do
  command -v "$tool" >/dev/null 2>&1 || { echo "필요한 도구가 없습니다: ${tool}" >&2; exit 1; }
done

eval_dir="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
repo_dir="$(CDPATH= cd -- "${eval_dir}/../.." && pwd)"
state_dir="${eval_dir}/.stack"

postgres="persona-quality-eval-pg"
owner_label="io.persona.quality-eval"
owner_value="persona-gateway/scripts/quality-eval/eval-stack.sh"

# --- 합성 자격증명 (실제 값 아님) ---
pg_user="eval_admin"
pg_password="eval_admin_not_real"
pg_db="persona_app"
bearer_token="quality-eval-token-not-real"
user_id="quality-eval-user"
display_name="골든셋 사용자"
cursor_key="quality-eval-cursor-key-not-real"

# 호스트 포트는 전부 127.0.0.1에만 연다. local-stack.sh(18080)와 겹치지 않게 고른다.
pg_port="${QUALITY_EVAL_PG_PORT:-15433}"
embedding_port="${QUALITY_EVAL_EMBEDDING_PORT:-18091}"
gateway_port="${QUALITY_EVAL_GATEWAY_PORT:-18090}"
embedding_dir="${repo_dir}/embedding-service"
default_gateway_src="${repo_dir}/python-backend"

# --- 소유 확인 ------------------------------------------------------------------

# 0=없음, 1=우리 것, 2=남의 것. 조회 자체가 실패하면(daemon 꺼짐 등) "없음"으로 바꾸지 않고 멈춘다.
container_ownership() {
  local label err
  err="$(mktemp)"
  if label="$(docker container inspect --format "{{index .Config.Labels \"${owner_label}\"}}" "$postgres" 2>"$err")"; then
    rm -f "$err"
    [ "$label" = "$owner_value" ] && return 1
    return 2
  fi
  if grep -q "No such container: ${postgres}" "$err"; then
    rm -f "$err"
    return 0
  fi
  echo "[중단] docker inspect 실패: $(cat "$err")" >&2
  rm -f "$err"
  exit 1
}

remove_postgres() {
  local state=0
  container_ownership || state=$?
  case $state in
    0) ;;
    1) docker rm --force "$postgres" >/dev/null ;;
    2)
      echo "[중단] ${postgres}은(는) 이 스크립트가 만든 컨테이너가 아닙니다. 지우지 않습니다." >&2
      exit 1
      ;;
  esac
}

# pid 파일의 프로세스가 아직 우리 uvicorn인지 확인하고 끝낸다. pid는 재사용될 수 있으므로
# 명령줄에 기대한 앱 이름이 없으면 다른 프로세스로 보고 건드리지 않는다.
stop_process() {
  local name="$1" app="$2" pid_file="${state_dir}/$1.pid" pid
  [ -f "$pid_file" ] || return 0
  pid="$(cat "$pid_file")"
  if kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o command= | grep -q "$app"; then
    kill "$pid"
    for _ in $(seq 1 30); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "주의: ${name}(pid ${pid})이 30초 안에 끝나지 않았습니다. 직접 확인하세요." >&2
      return 1
    fi
  fi
  rm -f "$pid_file"
}

# --- 기동 -----------------------------------------------------------------------

wait_http_ok() {
  local url="$1" seconds="$2" what="$3" log_file="$4"
  for _ in $(seq 1 "$seconds"); do
    if [ "$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 "$url")" = "200" ]; then
      return 0
    fi
    sleep 1
  done
  echo "[중단] ${what}이(가) ${seconds}초 안에 준비되지 않았습니다. 로그: ${log_file}" >&2
  tail -n 30 "$log_file" >&2 || true
  exit 1
}

database_url() {
  echo "postgresql://${pg_user}:${pg_password}@127.0.0.1:${pg_port}/${pg_db}"
}

start_postgres() {
  remove_postgres
  docker run --detach --name "$postgres" \
    --label "${owner_label}=${owner_value}" \
    --publish "127.0.0.1:${pg_port}:5432" \
    --env POSTGRES_USER="$pg_user" \
    --env POSTGRES_PASSWORD="$pg_password" \
    --env POSTGRES_DB="$pg_db" \
    pgvector/pgvector:pg16 >/dev/null
  # TCP로 확인한다. 초기화 중 임시 서버(Unix 소켓 전용)에 성공해 버리는 것을 피한다(local-stack.sh와 같은 이유).
  for _ in $(seq 1 60); do
    if docker exec --env PGPASSWORD="$pg_password" "$postgres" \
      psql --host 127.0.0.1 --username "$pg_user" --dbname "$pg_db" --tuples-only --command 'SELECT 1' >/dev/null 2>&1; then
      echo "  OK  Postgres (127.0.0.1:${pg_port})"
      return 0
    fi
    sleep 1
  done
  echo "[중단] Postgres가 준비되지 않았습니다." >&2
  exit 1
}

migrate() {
  local gateway_src="$1"
  (cd "$gateway_src" && DATABASE_URL="$(database_url)" uv run --locked alembic upgrade head >/dev/null)
  echo "  OK  alembic upgrade head (${gateway_src})"
}

start_embedding() {
  local log_file="${state_dir}/embedding.log"
  (cd "$embedding_dir" && uv sync --locked --quiet)
  # uv run이 아니라 venv의 uvicorn을 직접 띄운다 — pid 파일이 실제 서버 프로세스를 가리켜야
  # down에서 정확히 그 프로세스만 끝낼 수 있다.
  # `cd && nohup … &`로 쓰면 AND-list 전체가 서브셸로 백그라운드에 가서 $!가 그 서브셸을 가리키고,
  # 그 서브셸이 호출자의 stdout을 쥔 채 남는다. cd를 먼저 끝내고 서버만 백그라운드로 보낸다.
  (
    cd "$embedding_dir" || exit 1
    nohup .venv/bin/uvicorn --factory persona_embedding_service.main:create_app \
      --host 127.0.0.1 --port "$embedding_port" </dev/null >"$log_file" 2>&1 &
    echo $! >"${state_dir}/embedding.pid"
  )
  # 첫 기동은 모델 로딩(필요하면 내려받기)까지 기다린다.
  wait_http_ok "http://127.0.0.1:${embedding_port}/readyz" 300 "embedding-service" "$log_file"
  echo "  OK  embedding-service (127.0.0.1:${embedding_port})"
}

start_gateway() {
  local gateway_src="$1" log_file="${state_dir}/gateway.log"
  (cd "$gateway_src" && uv sync --locked --quiet)
  (
    cd "$gateway_src" || exit 1
    DATABASE_URL="$(database_url)" \
    PERSONA_EMBEDDING_URL="http://127.0.0.1:${embedding_port}" \
    PERSONA_STATIC_BEARER_TOKEN="$bearer_token" \
    PERSONA_STATIC_USER_ID="$user_id" \
    PERSONA_STATIC_DISPLAY_NAME="$display_name" \
    PERSONA_CURSOR_SIGNING_KEY="$cursor_key" \
    nohup .venv/bin/uvicorn --factory persona_minimal_api.main:create_app \
      --host 127.0.0.1 --port "$gateway_port" </dev/null >"$log_file" 2>&1 &
    echo $! >"${state_dir}/gateway.pid"
  )
  wait_http_ok "http://127.0.0.1:${gateway_port}/readyz" 60 "Gateway" "$log_file"
  echo "  OK  Gateway (127.0.0.1:${gateway_port}, src=${gateway_src})"
  echo "      mode=${PERSONA_CHAT_INFERENCE_MODE:-mock} prompt_version=${PERSONA_PROMPT_VERSION:-기본값}"
}

print_usage_hint() {
  echo
  echo "스택 준비 완료"
  echo "  QUALITY_EVAL_TOKEN=${bearer_token} uv run scripts/quality-eval/run.py \\"
  echo "    --base-url http://127.0.0.1:${gateway_port} --label <라벨> ..."
  echo "  정리: scripts/quality-eval/eval-stack.sh down"
}

parse_gateway_src() {
  local src="$default_gateway_src"
  while [ $# -gt 0 ]; do
    case "$1" in
      --gateway-src) src="$2"; shift 2 ;;
      *) echo "알 수 없는 인자: $1" >&2; exit 1 ;;
    esac
  done
  [ -f "${src}/alembic.ini" ] || { echo "[중단] python-backend 디렉터리가 아닙니다: ${src}" >&2; exit 1; }
  CDPATH= cd -- "$src" && pwd
}

up() {
  local gateway_src
  gateway_src="$(parse_gateway_src "$@")"
  down >/dev/null
  mkdir -p "$state_dir"
  echo "[1] Postgres";          start_postgres
  echo "[2] migration";         migrate "$gateway_src"
  echo "[3] embedding-service"; start_embedding
  echo "[4] Gateway";           start_gateway "$gateway_src"
  print_usage_hint
}

restart_gateway() {
  local gateway_src
  gateway_src="$(parse_gateway_src "$@")"
  stop_process gateway persona_minimal_api
  # 다른 소스로 바꿔 띄우면 그 소스의 migration head까지 올린다(같으면 아무 일도 없다).
  migrate "$gateway_src"
  start_gateway "$gateway_src"
}

down() {
  local failed=0
  stop_process gateway persona_minimal_api || failed=1
  stop_process embedding persona_embedding_service || failed=1
  remove_postgres
  if [ "$failed" -ne 0 ]; then
    echo "정리 미완료(위 주의 참고)" >&2
    return 1
  fi
  echo "정리 완료"
}

command="${1:-}"
[ $# -gt 0 ] && shift
case "$command" in
  up) up "$@" ;;
  restart-gateway) restart_gateway "$@" ;;
  down) down ;;
  *) echo "사용법: $0 up|restart-gateway [--gateway-src DIR] | down" >&2; exit 1 ;;
esac
