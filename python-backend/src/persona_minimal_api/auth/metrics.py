"""인증 경로 저카디널리티 메트릭.

- persona_auth_requests_total{endpoint, outcome}
  - endpoint: signup·login·logout
  - outcome: ok·invalid·locked·disabled·validation
    (invalid에는 틀린 아이디/비밀번호, 이미 쓰는 username, 무효 세션 로그아웃이 들어간다)

username·subject·토큰은 라벨에 넣지 않는다 — 카디널리티 폭발과 별개로, 그 자체가 사용자
식별값을 관측 계로 내보내는 것이기 때문이다(chat/metrics.py와 같은 규칙).
"""

from __future__ import annotations

from typing import Literal

from prometheus_client import Counter

AuthEndpoint = Literal["signup", "login", "logout"]
AuthOutcome = Literal["ok", "invalid", "locked", "disabled", "validation"]

AUTH_REQUESTS = Counter(
    "persona_auth_requests_total",
    "회원가입·로그인·로그아웃 요청 결과 수",
    ["endpoint", "outcome"],
)
