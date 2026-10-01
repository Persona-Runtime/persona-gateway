"""격리 스택에 합성 사용자 N명을 가입시키고 세션 토큰을 임시 파일에 모은다(동시접속 실험 준비).

사용법(저장소 루트에서):
    uv run --project python-backend python scripts/signup-synthetic-users.py \
        --base-url http://127.0.0.1:18080 --count 20

- 격리 스택 전용이다. 기본으로 loopback 주소(127.0.0.1·localhost·::1)만 허용한다 — 운영 주소에
  합성 계정을 만들지 않게 하려는 것이다. 운영 가입 경로는 공개라 막을 수단이 이것뿐이다.
- 아이디는 `lg<실행ID>_<번호>`, 비밀번호는 실행마다 새로 만든 난수다. 실제 사용자 값은 쓰지 않는다.
- 토큰은 화면·로그에 찍지 않고, 권한 0600 임시 파일에 JSON 줄(username·user_id·token)로만 남긴다.
  출력은 그 파일 경로뿐이다. 실험이 끝나면 파일을 지운다.
- 격리 스택 DB에도 platform grants(credentials·sessions)가 적용돼 있어야 가입이 된다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
import tempfile
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("signup-synthetic-users")

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# 계약상 username은 최대 32자다. "lg" + 실행ID 8자 + "_" + 번호 최대 5자 = 16자.
RUN_ID_HEX_CHARS = 8
MAX_USERS = 10000
REQUEST_TIMEOUT_SECONDS = 10.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base-url", required=True, help="Gateway 주소, 예: http://127.0.0.1:18080"
    )
    parser.add_argument(
        "--count", type=int, required=True, help=f"가입시킬 사용자 수(1~{MAX_USERS})"
    )
    return parser.parse_args()


def require_loopback(base_url: str) -> None:
    host = urlsplit(base_url).hostname
    if host not in LOOPBACK_HOSTS:
        raise SystemExit(f"격리 스택(loopback) 주소만 허용합니다: {host}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()
    require_loopback(args.base_url)
    if not 1 <= args.count <= MAX_USERS:
        raise SystemExit(f"--count는 1~{MAX_USERS} 사이여야 합니다.")

    run_id = secrets.token_hex(RUN_ID_HEX_CHARS // 2)
    # 비밀번호는 이 실행에서 다시 쓸 일이 없다(토큰을 바로 받는다). 기록하지 않는다.
    password = secrets.token_urlsafe(18)
    fd, path = tempfile.mkstemp(prefix=f"persona-loadgen-{run_id}-", suffix=".jsonl")
    os.fchmod(fd, 0o600)

    created = 0
    with (
        os.fdopen(fd, "w", encoding="utf-8") as out,
        httpx.Client(base_url=args.base_url, timeout=REQUEST_TIMEOUT_SECONDS) as client,
    ):
        for index in range(args.count):
            username = f"lg{run_id}_{index}"
            response = client.post(
                "/v1/auth/signup", json={"username": username, "password": password}
            )
            if response.status_code != 201:
                # 본문의 오류 코드만 보여 준다. 요청 값(아이디·비밀번호)은 다시 찍지 않는다.
                code = response.json().get("error", {}).get("code", "unknown")
                logger.error(
                    "가입 실패: index=%d status=%d code=%s", index, response.status_code, code
                )
                break
            body = response.json()
            record = {"username": username, "user_id": body["user"]["id"], "token": body["token"]}
            out.write(json.dumps(record) + "\n")
            created += 1

    logger.info("가입 %d/%d명, 토큰 파일: %s", created, args.count, path)
    return 0 if created == args.count else 1


if __name__ == "__main__":
    sys.exit(main())
