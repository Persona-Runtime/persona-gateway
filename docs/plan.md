# persona-gateway — 기획 문서

## 목적

온라인 request path를 담당하는 단일 Go module이다. workspace 경계 안에서 RAG context를 만들고 vLLM으로 SSE 요청을 전달하며, 단일 GPU overload를 제어한다.

## 책임

- OpenAPI와 `/v1/chat/completions`, `/healthz`, `/readyz`, `/metrics`
- workspace 검증, PostgreSQL metadata 조회, Qdrant filtered retrieval
- tokenizer 기반 context budget과 citation ID 생성
- vLLM 요청/SSE bridge, client disconnect cancellation 전파
- bounded queue, token credit, interactive/background DRR admission
- circuit breaker와 `GPU_BACKEND_UNAVAILABLE` 503
- Prometheus metrics, OpenTelemetry tracing

## 제외

- ingestion/parser, Kubernetes manifest, dashboard/alert rule
- model lifecycle, embedding model serving, multi-model routing
- actual raw source를 API로 반환하는 기능

## API 기본 계약

```text
POST /v1/chat/completions
  request: workspace_id, messages, stream, max_output
  SSE: delta | citations | done | error

400 invalid input/token budget
403 workspace boundary violation
429 bounded queue overload + Retry-After
503 GPU_BACKEND_UNAVAILABLE
504 upstream deadline
```

## 구현 루프

### Loop 1 — HTTP/SSE skeleton

- in-memory fake retriever/vLLM으로 request validation과 SSE stream을 구현한다.
- 완료 조건: `go test -race` 및 cancellation test 통과.

### Loop 2 — RAG contract

- workspace filter, top-k dedupe, actual tokenizer token budget, citation ID를 구현한다.
- 완료 조건: cross-workspace retrieval은 403이며 golden synthetic Q/A citation test가 통과.

### Loop 3 — vLLM boundary

- vLLM health/readiness 구분, upstream deadline, stream error mapping을 구현한다.
- 완료 조건: backend down이면 target 시간 안에 503, prompt body가 telemetry에 없음.

### Loop 4 — overload control

- static token credit + bounded DRR를 FIFO baseline과 비교한다.
- 완료 조건: fixed synthetic trace에서 interactive HoL과 queue growth를 기록 가능.

### Loop 5 — production observability

- request-id를 trace에만 연결하고 queue/reject/cancel/context-build/upstream-first-byte/E2E metric을 만든다.
- 완료 조건: Prometheus label/trace attribute에 prompt 또는 retrieved text가 없음.

## 검증 기준

- OpenAPI contract test와 deterministic synthetic SSE integration test
- `go test -race`, cancellation leak 및 bounded queue test
- workspace isolation, 429 retry contract, 503 circuit-breaker test
- vLLM image/model/tokenizer/template revision이 experiment record에 남음
