#!/usr/bin/env bash
set -eu

# 역할이 분리된 실제 DB 위에서 Gateway를 띄운다. Web의 실제 HTTP 검사가 여기에 붙는다.
#
# smoke-container.sh와 책임이 다르다. 그쪽은 "이미지가 비루트·read-only로 동작하는가"를 보고,
# 이 스크립트는 "migrator/runtime 권한이 나뉜 DB 위에서 계약대로 동작하는가"를 본다.
#
# 사용법:
#   scripts/local-stack.sh up [image]   # 기동하고 접속 주소를 출력한다
#   scripts/local-stack.sh restart      # Gateway만 재시작한다(저장이 실제 DB인지 확인용)
#   scripts/local-stack.sh down         # 이 스크립트가 만든 것만 지운다
#
# 실제 사용자 자료·운영 Secret을 쓰지 않는다. 아래 값은 전부 이 실행 전용 합성 값이다.

for tool in docker curl awk; do
  command -v "$tool" >/dev/null 2>&1 || { echo "필요한 도구가 없습니다: ${tool}" >&2; exit 1; }
done

repo_dir="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
workspace_dir="$(CDPATH= cd -- "${repo_dir}/.." && pwd)"

prefix="persona-local-stack"
network="${prefix}-net"
postgres="${prefix}-pg"
api="${prefix}-api"

# 소유 표시. 이름이 같다는 것은 소유의 근거가 아니므로, 지우기 전에 이 라벨을 확인한다.
# 라벨이 없는 동명 컨테이너는 다른 사람 것일 수 있다.
owner_label="io.persona.local-stack"
owner_value="persona-gateway/scripts/local-stack.sh"

# --- 합성 자격증명 (실제 값 아님) ---
superuser="stack_admin"
super_password="stack_admin_not_real"
migrator_password="stack_migrator_not_real"
runtime_password="stack_runtime_not_real"
# 이 세 값은 api.live.test.ts와의 계약이다. 그쪽이 대상이 합성 스택인지 신원으로 대조하므로
# 한쪽만 바꾸면 검사가 "운영 대상"으로 판단해 중단한다. 함께 고친다.
bearer_token="stack-bearer-token-not-real"
user_id="stack-user-0001"
display_name="합성 사용자"
cursor_key="stack-cursor-signing-key-not-real"

# grants 파일이 `GRANT CONNECT ON DATABASE persona_app`처럼 DB 이름을 직접 적는다.
# 이름을 바꿔 적용하면 그 파일이 아니라 고쳐 쓴 사본을 검증하게 되므로 실제 이름을 쓴다.
db_name="persona_app"
migrator_role="persona_migrator"
runtime_role="persona_runtime"
grants_file="${workspace_dir}/persona-platform/db/grants/persona_minimal.sql"

# 컨테이너를 만들 때만 쓰는 호스트 포트. 이후 주소는 gateway_url()이 컨테이너에서 읽으므로
# 이 변수가 없거나 달라져도 재시작 대상이 흔들리지 않는다.
# 이미 쓰는 포트면 docker가 분명한 오류로 멈춘다. 다른 값이 필요하면 환경변수로 준다.
host_port="${PERSONA_STACK_PORT:-18080}"

# psql은 **TCP로** 붙는다. 공식 postgres 이미지는 초기화 중 임시 서버를 띄우는데 그것은
# Unix 소켓 전용이다. 소켓으로 SELECT 1을 걸면 그 임시 서버에서도 성공해, 곧이어 임시 서버가
# 내려가면서 role 생성·migration과 경합한다. TCP 접속 성공이 최종 서버 기동의 증거다.
psql_super() {
  docker exec --interactive --env PGPASSWORD="$super_password" "$postgres" \
    psql --host 127.0.0.1 --username "$superuser" --dbname "$db_name" --no-psqlrc --quiet "$@"
}

# 소유 판정: 0=없음, 1=우리 것, 2=남의 것, 3=조회 불능.
#
# **조회 실패를 "없음"으로 바꾸지 않는다.** daemon 연결 실패·권한 오류까지 부재로 보면,
# Docker가 아예 죽어 있어도 down이 "정리 완료"와 종료 코드 0을 돌려준다.
# docker는 두 경우 모두 exit 1이므로 메시지로 갈라야 한다.
inspect_label() {
  local kind="$1" name="$2" err out template absent
  # 라벨이 놓인 자리가 다르다. 컨테이너는 .Config.Labels, 네트워크는 .Labels다.
  # 한쪽 템플릿을 양쪽에 쓰면 템플릿 오류가 나고, 그것이 소유 판정처럼 보인다.
  #
  # "없음" 판정 문구도 종류별로 다르게 고정한다. "not found"/"No such object"처럼 넓은
  # 문구만 보면, docker context·연결 설정 오류에 우연히 같은 단어가 섞여도 "자원이 없다"로
  # 잘못 읽는다. 이 대상의 종류와 이름이 정확히 들어간 문장만 "없음"으로 인정한다.
  case "$kind" in
    container)
      template="{{index .Config.Labels \"${owner_label}\"}}"
      absent="No such container: ${name}"
      ;;
    network)
      template="{{index .Labels \"${owner_label}\"}}"
      absent="network ${name} not found"
      ;;
    *) echo "[중단] 알 수 없는 대상 종류: ${kind}" >&2; return 20 ;;
  esac
  err="$(mktemp)"
  if out="$(docker "$kind" inspect --format "$template" "$name" 2>"$err")"; then
    rm -f "$err"
    printf '%s' "$out"
    return 0
  fi
  local message
  message="$(cat "$err")"; rm -f "$err"
  case "$message" in
    *"$absent"*) return 10 ;;
    *) echo "[중단] docker ${kind} inspect 실패: ${message}" >&2; return 20 ;;
  esac
}

ownership_of() {
  local kind="$1" name="$2" label state=0
  label="$(inspect_label "$kind" "$name")" || state=$?
  case $state in
    0) [ "$label" = "$owner_value" ] && return 1; return 2 ;;
    10) return 0 ;;
    *) return 3 ;;
  esac
}

# 컨테이너와 네트워크를 같은 규칙으로 다룬다. 지난번에는 네트워크만 소유 확인이 빠져,
# 동명의 남의 네트워크를 지우거나 그대로 재사용했다.
remove_owned() {
  local kind="$1" name="$2" state=0
  # set -e가 0이 아닌 반환에서 멈추지 않도록 상태만 받아 둔다.
  ownership_of "$kind" "$name" || state=$?
  case $state in
    0) return 0 ;;  # 없으면 지울 것도 없다
    1)
      if [ "$kind" = network ]; then
        # 삭제 실패 원인은 붙어 있는 컨테이너만이 아닐 수 있다(권한 문제 등). 원인을
        # 단정하지 않고 docker가 실제로 낸 메시지를 그대로 보여준 뒤, 붙어 있는 컨테이너는
        # 참고 정보로 별도로 조회해 덧붙인다.
        local rm_err
        rm_err="$(mktemp)"
        if ! docker network rm "$name" >/dev/null 2>"$rm_err"; then
          local rm_message attached
          rm_message="$(cat "$rm_err")"; rm -f "$rm_err"
          attached="$(docker network inspect "$name" \
            --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null || true)"
          echo "주의: ${name} 삭제 실패: ${rm_message}" >&2
          if [ -n "$attached" ]; then
            echo "      현재 붙어 있는 컨테이너(참고): ${attached}" >&2
          fi
          return 1
        fi
        rm -f "$rm_err"
      else
        docker rm --force "$name" >/dev/null  # 오류를 숨기지 않는다
      fi
      ;;
    2)
      echo "[중단] ${name}은(는) 이 스크립트가 만든 ${kind}이(가) 아닙니다." >&2
      echo "        이름이 같아도 지우지 않습니다. 직접 확인한 뒤 치워 주세요." >&2
      exit 1
      ;;
    3) exit 1 ;;  # 조회 불능. 이유는 inspect_label이 이미 출력했다
  esac
}

down() {
  local left=0
  remove_owned container "$api"
  remove_owned container "$postgres"
  # network는 마지막이다. 위 컨테이너를 지워야 비어서 삭제된다.
  remove_owned network "$network" || left=$?
  if [ "$left" -ne 0 ]; then
    echo "정리 미완료: ${prefix}-* (위 주의 참고)" >&2
    return 1
  fi
  echo "정리 완료: ${prefix}-*"
}

# 이 실행에서 만든 그 컨테이너가 맞는지 id로 대조한다. 존재와 이미지 이름만 보면
# 같은 이름의 다른 컨테이너를 제 것으로 착각한다.
guard_is_ours() {
  local name="$1" expected_id="$2" actual_id
  # 조회에 실패하면 빈 값이 되어 아래 비교에서 걸린다. 실패를 통과로 바꾸지 않는다.
  actual_id="$(docker inspect --format '{{.Id}}' "$name" 2>/dev/null || true)"
  if [ "$actual_id" != "$expected_id" ]; then
    echo "[중단] ${name}이(가) 이 실행에서 만든 컨테이너와 다릅니다." >&2
    exit 1
  fi
}

wait_for_gateway() {
  local base_url="$1"
  for _ in $(seq 1 30); do
    if [ "$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 "${base_url}/readyz")" = "200" ]; then
      return 0
    fi
    sleep 1
  done
  echo "[중단] /readyz가 200이 되지 않았습니다." >&2
  docker logs "$api" >&2 || true
  exit 1
}

# 환경변수가 아니라 **컨테이너의 실제 게시 주소**를 읽는다.
# 변수를 다시 읽으면, 다른 포트로 띄운 스택을 재시작할 때 엉뚱한 포트를 기다린다.
gateway_url() {
  local published
  published="$(docker port "$api" 8080/tcp 2>/dev/null | head -1)"
  if [ -z "$published" ]; then
    echo "[중단] ${api}의 게시 포트를 읽지 못했습니다." >&2
    exit 1
  fi
  echo "http://${published}"
}

start_gateway() {
  local image="$1" runtime_url gateway_id
  runtime_url="postgresql://${runtime_role}:${runtime_password}@${postgres}:5432/${db_name}"
  # PERSONA_EMBEDDING_URL은 config.py에서 기본값 없는 필수 필드다 — 없으면 앱이 시작
  # 시점에 죽는다. 이 로컬 스택은 임베딩 서비스를 띄우지 않으므로 존재만 하면 되는
  # 더미 값을 준다(실제로 그 주소에 연결하지 않는다).
  gateway_id="$(docker run --detach --name "$api" --network "$network" \
    --label "${owner_label}=${owner_value}" \
    --user 10001:10001 \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,uid=10001,gid=10001,mode=1700 \
    --publish "127.0.0.1:${host_port}:8080" \
    --env DATABASE_URL="$runtime_url" \
    --env PERSONA_STATIC_BEARER_TOKEN="$bearer_token" \
    --env PERSONA_STATIC_USER_ID="$user_id" \
    --env PERSONA_STATIC_DISPLAY_NAME="$display_name" \
    --env PERSONA_CURSOR_SIGNING_KEY="$cursor_key" \
    --env PERSONA_EMBEDDING_URL="http://127.0.0.1:8081" \
    "$image")"
  guard_is_ours "$api" "$gateway_id"
}

up() {
  local image="${1:-persona-minimal-api:local-arm64}"

  # 먼저 소유를 확인하고 지운다. down()이 라벨을 보므로 남의 것이면 여기서 멈춘다.
  down >/dev/null

  echo "  host arch    : $(uname -m)"
  echo "  docker server: $(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')"
  docker image inspect "$image" --format "  image        : ${image} ({{.Os}}/{{.Architecture}})" 2>/dev/null || {
    echo "[중단] 이미지가 없습니다: ${image}" >&2; exit 1; }

  echo
  echo "[1] 격리 network와 Postgres"
  # 이미 있는 network를 확인 없이 재사용하지 않는다. 남의 것이면 붙지도 않는다.
  local net_state=0
  ownership_of network "$network" || net_state=$?
  case $net_state in
    0) docker network create --label "${owner_label}=${owner_value}" "$network" >/dev/null ;;
    1) echo "  기존 network 재사용: ${network}" ;;
    2)
      echo "[중단] ${network}은(는) 이 스크립트가 만든 network가 아닙니다." >&2
      echo "        남의 network에 컨테이너를 붙이지 않습니다." >&2
      exit 1
      ;;
    3) exit 1 ;;
  esac
  # 호스트 포트를 공개하지 않는다. DB는 이 network 안에서만 접근한다.
  # postgres:16-alpine이 아니라 pgvector/pgvector:pg16을 쓴다 — 0003부터 head
  # migration이 pgvector 확장을 요구한다(material_chunks.embedding).
  local postgres_id
  postgres_id="$(docker run --detach --name "$postgres" --network "$network" \
    --label "${owner_label}=${owner_value}" \
    --env POSTGRES_USER="$superuser" \
    --env POSTGRES_PASSWORD="$super_password" \
    --env POSTGRES_DB="$db_name" \
    pgvector/pgvector:pg16)"
  guard_is_ours "$postgres" "$postgres_id"

  local ready=false
  for _ in $(seq 1 60); do
    if psql_super --tuples-only --command 'SELECT 1' >/dev/null 2>&1; then
      ready=true; break
    fi
    sleep 1
  done
  [ "$ready" = true ] || { echo "[중단] Postgres가 준비되지 않았습니다." >&2; exit 1; }
  echo "  OK  Postgres 기동 (TCP 질의 성공 · 호스트 포트 비공개)"

  echo
  echo "[2] migrator/runtime 역할 분리"
  # 한 계정으로 돌리면 grants가 실행 경로에서 한 번도 검증되지 않고,
  # runtime이 DDL을 할 수 있는지도 드러나지 않는다.
  psql_super --command "CREATE ROLE ${migrator_role} LOGIN PASSWORD '${migrator_password}'" >/dev/null
  psql_super --command "CREATE ROLE ${runtime_role} LOGIN PASSWORD '${runtime_password}'" >/dev/null
  psql_super --command "GRANT CREATE, CONNECT ON DATABASE ${db_name} TO ${migrator_role}" >/dev/null
  psql_super --command "REVOKE CREATE ON SCHEMA public FROM PUBLIC" >/dev/null
  echo "  OK  ${migrator_role} · ${runtime_role} 생성"

  echo
  echo "[3] migrator 자격으로 migration"
  local migrator_url applied expected
  migrator_url="postgresql://${migrator_role}:${migrator_password}@${postgres}:5432/${db_name}"
  docker run --rm --network "$network" \
    --entrypoint /app/.venv/bin/alembic \
    --env DATABASE_URL="$migrator_url" \
    "$image" upgrade head >/dev/null
  # 기대 revision을 여기 적지 않고 소스에서 읽는다. 상수로 박으면 새 migration마다 이 스크립트가 실패한다.
  expected="$(ls "$repo_dir/python-backend/migrations/versions"/*.py | sort | tail -1 | xargs basename | sed 's/\.py$//')"
  applied="$(psql_super --tuples-only --command 'SELECT version_num FROM persona_minimal.alembic_version' | tr -d '[:space:]')"
  if [ "$applied" != "$expected" ]; then
    echo "[중단] revision이 소스의 head와 다릅니다: ${applied} (소스 head ${expected})" >&2
    exit 1
  fi
  echo "  OK  alembic upgrade head → ${applied}"

  echo
  echo "[4] grants 적용"
  # 파일이 없으면 건너뛰지 않고 멈춘다. 권한 없이 통과한 검증은 검증이 아니다.
  if [ ! -f "$grants_file" ]; then
    echo "[중단] grants 파일이 없습니다: ${grants_file}" >&2
    echo "        단독 checkout이면 persona-platform을 나란히 두고 다시 실행하세요." >&2
    exit 1
  fi
  psql_super --file - < "$grants_file" >/dev/null
  echo "  OK  $(basename "$grants_file") 적용"

  echo
  echo "[5] runtime 자격으로 Gateway 기동"
  start_gateway "$image"
  local base_url
  base_url="$(gateway_url)"
  wait_for_gateway "$base_url"
  echo "  OK  /readyz 200 (runtime 권한으로 스키마·revision 확인)"

  echo
  echo "스택 준비 완료"
  echo "  PERSONA_LIVE_API=${base_url}"
  echo "  PERSONA_LIVE_TOKEN=${bearer_token}"
  echo
  echo "  실제 HTTP 검사: PERSONA_LIVE_API=${base_url} PERSONA_LIVE_TOKEN=${bearer_token} \\"
  echo "                  npx vitest run src/lib/api.live.test.ts"
  echo "  Web dev 서버:   VITE_LOCAL_API_TARGET=${base_url} npm run dev"
  echo "  정리:           scripts/local-stack.sh down"
}

# Gateway만 다시 띄운다. 저장이 앱 메모리가 아니라 DB에 있는지 확인하는 데 쓴다.
# 같은 프로세스에서 앱 객체만 다시 만드는 것과 다른 검증이다.
restart() {
  local state=0
  ownership_of container "$api" || state=$?
  [ "$state" -eq 1 ] || { echo "[중단] 재시작할 Gateway가 이 스크립트의 것이 아닙니다." >&2; exit 1; }
  docker restart "$api" >/dev/null
  local base_url
  base_url="$(gateway_url)"
  wait_for_gateway "$base_url"
  echo "재시작 완료: ${base_url}"
}

# 이 스크립트가 관리하는 Gateway의 주소만 출력한다. 검사가 "내가 조회하는 주소"와
# "내가 재시작할 컨테이너의 주소"를 대조하는 데 쓴다. 신원만으로는 두 합성 스택을 구분하지 못한다.
url() {
  local state=0
  ownership_of container "$api" || state=$?
  [ "$state" -eq 1 ] || { echo "[중단] 이 스크립트가 관리하는 Gateway가 없습니다." >&2; exit 1; }
  gateway_url
}

# daemon에 닿는지 먼저 본다. 여기서 막히면 아래 조회들이 제각기 다른 모습으로 실패한다.
docker version --format '{{.Server.Os}}' >/dev/null 2>&1 || {
  echo "[중단] Docker daemon에 연결하지 못했습니다." >&2; exit 1; }

case "${1:-up}" in
  up) shift || true; up "${1:-}" ;;
  restart) restart ;;
  url) url ;;
  down) down ;;
  *) echo "사용법: $0 [up [image] | restart | url | down]" >&2; exit 1 ;;
esac
