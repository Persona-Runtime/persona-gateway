# 답변 품질 골든셋 (quality-eval)

프롬프트·검색·모델을 바꿀 때마다 **같은 25문항**으로 전후를 비교하기 위한 도구다.
합성 캐릭터 "민서"(바닷가 서점 직원) 하나와 질문 25개, 실행기(`run.py`), 채점기(`score.py`),
격리 스택(`eval-stack.sh`)으로 이뤄진다.

## 지켜야 할 전제

- **운영에 돌리지 않는다.** `run.py`는 대상 주소가 loopback·사설 IP·`localhost`가 아니면 시작하지
  않는다. Gateway가 붙는 vLLM도 격리된 것만 쓴다(`PERSONA_VLLM_BASE_URL`에 운영 주소 금지).
- 자료·질문은 **전부 지어낸 합성 텍스트**다. 실제 캐릭터 원문·운영 DB 데이터를 넣지 않는다.
- 심판(`score.py`)을 외부 API로 지정하면 답변 원문과 질문이 그 API로 간다. **합성 자료로 돌린
  결과만** 채점한다는 전제에서만 허용한다.
- 결과 jsonl(`results/*.jsonl`)은 커밋하지 않는다(루트 `.gitignore`). 비교에 쓸 summary만 PR에
  첨부한다.

## 구성

| 파일 | 역할 |
| --- | --- |
| `fixtures/minseo/` | profile(약 600자) · events(약 6,000자, 헤딩 12개) · speech_examples(44줄, 반말) · relationships |
| `questions.yaml` | fact 10 · unknown 5 · followup 5 · style 5. 필드 설명은 파일 머리 주석 |
| `run.py` | 캐릭터 생성 → 자료 → 색인 → 활성화 → 문항마다 새 대화로 질문, SSE 수집 → `results/<UTC>-<label>.jsonl` |
| `score.py` | 규칙 지표 + LLM 심판 5축 → `results/summary-<label>.md`, `*.scored.jsonl` |
| `eval-stack.sh` | Postgres(컨테이너) + embedding-service + Gateway(소스 디렉터리에서 실행) |

후속 질문(followup)의 `history`에서 assistant 턴은 참고용이다. API로는 assistant 턴을 주입할 수
없으므로 `run.py`는 user 턴을 같은 대화에 **실제로 먼저 질문**하고, 모델이 한 답을
`actual_history`로 남긴다. 앞 턴이 실패하면 그 문항은 `history_failed`로 기록하고 본 질문을 하지 않는다.

## 실행

요구 도구: docker, uv, curl. `run.py`·`score.py`는 PEP 723 인라인 의존성(httpx, pyyaml)이라
`uv run`이 알아서 설치한다. python-backend 의존성은 늘리지 않는다.

```bash
# 1) 격리 스택 — Gateway 설정은 이 셸의 env를 물려받는다
PERSONA_CHAT_INFERENCE_MODE=llm \
PERSONA_VLLM_BASE_URL=http://<격리 vLLM 주소> \
PERSONA_VLLM_MODEL=<served-model-name> \
PERSONA_PROMPT_VERSION=v2 \
scripts/quality-eval/eval-stack.sh up                     # 기본 --gateway-src는 이 저장소 python-backend

# 2) 실행 — 라벨 구성값(--prompt-version 등)은 Gateway가 알려주지 않으므로 직접 적는다
QUALITY_EVAL_TOKEN=quality-eval-token-not-real \
uv run scripts/quality-eval/run.py --base-url http://127.0.0.1:18090 \
  --label v2-b4096-s07 --prompt-version v2 --budget 4096 --sampling s07 --model <model>

# 3) 채점 — 심판은 OpenAI 호환 엔드포인트 하나
QUALITY_JUDGE_BASE_URL=http://<심판 주소> QUALITY_JUDGE_MODEL=<모델> \
uv run scripts/quality-eval/score.py scripts/quality-eval/results/<파일>.jsonl
# (QUALITY_JUDGE_API_KEY는 필요할 때만. 규칙 지표만 보려면 --no-judge)

# 4) 정리
scripts/quality-eval/eval-stack.sh down
```

설정이나 코드만 바꿔 다시 잴 때는 스택 전체를 내리지 않고 Gateway만 다시 띄운다.

```bash
PERSONA_CHAT_INFERENCE_MODE=llm ... PERSONA_PROMPT_VERSION=v1 \
scripts/quality-eval/eval-stack.sh restart-gateway --gateway-src <다른 checkout>/python-backend
```

baseline(변경 전 코드)은 `git worktree add <경로> origin/develop`로 받은 checkout을
`--gateway-src`로 지정해 같은 스택에서 잰다.

mock 모드(`PERSONA_CHAT_INFERENCE_MODE`를 비우거나 `mock`)로도 끝까지 돈다(배선 확인용).
mock 결과는 답변을 채점하지 않고, 검색은 실제 임베딩으로 돌기 때문에 검색 지표만 남긴다.

## 라벨 규칙

`<prompt_version>-b<budget>-<sampling>` 형식으로 쓴다.

| 라벨 | 뜻 |
| --- | --- |
| `v1-b4096-default` | 프롬프트 v1, BUDGET_4096, 샘플링 미지정(vLLM generation_config 기본값). 변경 전 develop baseline |
| `v2-b4096-s07` | 프롬프트 v2, BUDGET_4096, 명시 샘플링(temperature 0.7 · top_p 0.8 · top_k 20 · min_p 0 · presence 1.0) |
| `v1-b4096-s07` | 프롬프트 효과와 샘플링 효과를 나눠 볼 때 |

임베딩·모델을 바꾸면 라벨 뒤에 붙인다(예: `v2-b16384-s07-bgem3`).

## 지표

- **LLM 심판 5축(1~5)**: 근거 정확성, 지어냄 없음, 캐릭터 일관성, 한국어 자연스러움, 길이·형식.
  응답이 JSON이 아니면 `judge_error`로 표에 그대로 보이고 평균에서 빠진다(0점으로 바꾸지 않는다).
  같은 vLLM을 심판으로 쓰면 자기 편향이 있어 라벨 간 **상대 비교**로만 읽는다.
- **규칙 지표(LLM 없음)**: 금지 문자열 포함률(공통 금지어 + 문항별 `must_not`), 문장 수,
  한글 외 글자 비율, 검색 recall@5(기대 heading_path가 상위 5개 본문 citation에 있는 비율),
  citation precision. 금지어는 신호일 뿐이다("AI라니 무슨 소리야"도 걸린다).
