"""argon2id 비밀번호 해시와 검증.

hash·verify는 의도적으로 느린(수십 ms) CPU 작업이다. 호출하는 라우트를 sync `def`로 두면
FastAPI가 스레드풀에서 실행하므로 이벤트 루프를 막지 않는다(기존 DB 호출 라우트와 같은 방식).
"""

from __future__ import annotations

from functools import cached_property

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# 지시문 기준값. memory_cost 단위는 KiB라 64 MiB다. 값을 바꾸면 이미 저장된 해시는 그 해시에 적힌
# 파라미터로 계속 검증되므로 기존 사용자가 로그인하지 못하게 되지는 않는다.
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST_KIB = 65536
ARGON2_PARALLELISM = 1

# 없는 아이디로 로그인할 때 verify할 대상의 원문. 비밀값이 아니다 — 이 해시와 일치하는
# 사용자는 존재하지 않으며, 목적은 "검증 1회와 같은 시간"을 쓰는 것뿐이다.
_DUMMY_PASSWORD = "persona-dummy-password-for-timing"


class PasswordHashing:
    """비밀번호를 PHC 문자열로 만들고 검증한다.

    파라미터를 주입할 수 있게 둔 것은 테스트가 아니라 운영 조정을 위해서다. 테스트도 기본값을
    써서, 응답 시간 비교가 실제 비용으로 이뤄지게 한다.
    """

    def __init__(
        self,
        time_cost: int = ARGON2_TIME_COST,
        memory_cost_kib: int = ARGON2_MEMORY_COST_KIB,
        parallelism: int = ARGON2_PARALLELISM,
    ) -> None:
        # argon2-cffi의 기본 type이 argon2id다.
        self._hasher = PasswordHasher(
            time_cost=time_cost, memory_cost=memory_cost_kib, parallelism=parallelism
        )

    @cached_property
    def _dummy_hash(self) -> str:
        # 첫 "없는 아이디" 로그인 때 한 번 만든다. 기동 시간에 해시 비용을 더하지 않으려는 것이다.
        return self._hasher.hash(_DUMMY_PASSWORD)

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        """일치하면 True. 불일치·손상된 해시는 모두 False다(어느 쪽이든 로그인 실패)."""
        try:
            return self._hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            # VerifyMismatchError(불일치)는 VerificationError의 하위 클래스다.
            return False

    def verify_against_dummy(self, password: str) -> None:
        """없는 아이디에서도 verify 1회 비용을 쓴다 — 응답 시간으로 계정 존재를 알 수 없게."""
        self.verify(self._dummy_hash, password)
