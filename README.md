# persona-gateway

온라인 request path를 담당하는 단일 Go module. workspace 경계 안에서 RAG context를
만들고 vLLM으로 SSE 요청을 전달하며, 단일 GPU overload를 제어한다.

기획 문서: [`docs/repository-plans/persona-gateway.md`](../docs/repository-plans/persona-gateway.md)

## 디렉토리

```
api/                    # OpenAPI 스펙
cmd/gateway/            # 서버 진입점
internal/
├── config/             # 설정 로드/검증
├── httpapi/            # 라우팅, 요청 검증, 오류 매핑
├── sse/                # delta | citations | done | error 스트림
├── retrieval/          # Qdrant filtered retrieval, workspace filter
├── context/            # tokenizer 기반 context budget, citation ID
├── vllm/               # upstream client, health/readiness, deadline
├── admission/          # bounded queue, token credit, DRR
└── telemetry/          # Prometheus metrics, OTel tracing
test/fixtures/
├── sse/                # 합성 SSE 스트림
└── golden_qa/          # 합성 Q/A citation 기대값
```

## 하지 않는 것

- ingestion/parser, Kubernetes manifest, dashboard/alert rule
- model lifecycle, embedding model serving, multi-model routing
- 실제 원문을 API로 반환하는 기능

## API 계약 (요약)

```
POST /v1/chat/completions   workspace_id, messages, stream, max_output
SSE  delta | citations | done | error
GET  /healthz  /readyz  /metrics

400 입력/토큰 예산 위반   403 workspace 경계 위반
429 queue overload + Retry-After
503 GPU_BACKEND_UNAVAILABLE   504 upstream deadline
```

## 관측 원칙

prompt 본문과 retrieved text는 metric label, log, trace attribute에 **넣지 않는다**.
request-id는 trace 상관관계용으로만 쓴다.

## 구현 루프

1. HTTP/SSE 스켈레톤 (fake retriever/vLLM, `go test -race`, cancellation)
2. RAG 계약 (workspace filter, top-k dedupe, 실제 tokenizer 예산, citation ID)
3. vLLM 경계 (health/readiness 구분, upstream deadline, 오류 매핑)
4. overload 제어 (token credit + bounded DRR vs FIFO baseline)
5. production observability (queue/reject/cancel/first-byte/E2E metric)

현재 상태: 뼈대만 존재.
