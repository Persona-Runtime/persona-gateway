"""임베딩 서비스 설정 — 인수인계 §4-4/§8 Q2. 값 대부분은 코드 상수다: 이 서비스는
비밀도 배포 대상도 스스로 고르지 않고, 계약이 고정한 값만 쓴다.

OMP_NUM_THREADS는 반드시 torch가 처음 import되기 전에 정해져 있어야 한다 — 이후
바꿔도 이미 초기화된 스레드 풀에는 적용되지 않는다. 이 모듈이 `model`보다 먼저
import되어야 하는 이유가 그것이다.
"""

from __future__ import annotations

import os

# torch/OpenMP가 이미 스레드 수를 정했다면 존중하고, 없으면 기본값을 준다. 코어를
# 몇 개 요청하든(k8s resources.requests/limits) 상관없이 과도한 스레드 경합을
# 막으려는 잠정값 — 실측 후 조정한다.
os.environ.setdefault("OMP_NUM_THREADS", "2")

MODEL_NAME = "intfloat/multilingual-e5-small"
# 허깅페이스 허브 커밋 해시로 고정한다 — 태그(main)를 쓰면 업스트림이 모델을
# 바꿔치기해도 우리는 모른 채로 다른 임베딩을 계산하게 된다. 조회한 시점:
# https://huggingface.co/api/models/intfloat/multilingual-e5-small
MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
EMBEDDING_DIM = 384

MAX_TEXTS_PER_REQUEST = 64
MAX_CHARS_PER_TEXT = 2000

# e5 계열은 입력에 역할 prefix가 없으면 품질이 떨어진다 — 호출자가 매번 붙이게
# 하면 언젠가 빠뜨리는 호출이 생기므로 서비스가 강제한다(계약 §4-4).
PASSAGE_PREFIX = "passage: "
QUERY_PREFIX = "query: "
