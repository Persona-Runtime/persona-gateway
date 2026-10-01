"""계정(username·비밀번호)과 세션 토큰 인증.

읽는 순서: service.py(가입·로그인·로그아웃·세션 판정 규칙) → store.py(DB 읽기·쓰기와 잠금)
→ passwords.py(argon2 해시) → metrics.py. HTTP 라우트와 인증 순서는 main.py에 있다.
"""
