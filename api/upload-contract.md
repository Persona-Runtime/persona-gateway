# Upload API — 1단계

이 문서는 기존 3개 route의 구현 참고 계약이다. 최신 목표는 [전체 API v1](service-api-v1.md)과 [OpenAPI](openapi.json).
초안/revision/멱등 키 등 신규 규칙이 실제 코드에 반영됐다는 뜻은 아니다. [구현 차이](implementation-gap-v1.md)를 확인한다.

모든 요청은 Authorization Bearer static-token이 필요하다. 이 토큰은 Tailnet 단일 사용자용
Kubernetes Secret에서만 제공하며, 요청 헤더의 사용자 ID는 신뢰하지 않는다.

## Persona

POST /v1/personas는 JSON name 필드를 받는다. 이름은 비어 있지 않은 UTF-8 문자열이며 같은
사용자 안에서 유일하다. 성공은 201과 id, name, created_at을 반환하고 중복은 409
duplicate_persona_name이다.

## 업로드

POST /v1/personas/{persona_id}/uploads는 multipart/form-data다. 붙여넣기 필드는 profile,
events, relationships, abilities, speech_examples이고, 파일은 각각 반복 가능한
files.profile, files.events, files.relationships, files.abilities, files.speech_examples에
넣는다. 범용 files 필드는 지원하지 않는다.

profile은 텍스트 또는 파일 중 하나 이상으로 비어 있지 않아야 한다. 파일은 UTF-8 .txt/.md,
파일당 최대 1 MiB이며 최대 20개다. 모든 텍스트·파일 원문의 합계는 5 MiB, multipart 포장
전체는 6 MiB다. 붙여넣기 필드에는 파일별 1 MiB 한도를 적용하지 않는다.

성공은 202와 job_id, version_id, queued 상태다. 오류는 항상 error 필드를 가진 JSON이다.

| 상태 | 코드 |
| --- | --- |
| 토큰 없음/불일치 | 401 unauthorized |
| persona 없음 또는 타인 소유 | 404 persona_not_found |
| 본문이 multipart가 아님 | 400 invalid_multipart |
| 포장 또는 원문 총량 초과 | 413 request_too_large / total_text_too_large |
| 지원하지 않는 파일 확장자 | 415 unsupported_file_type |
| 빈/비 UTF-8 입력, profile 누락, 미지정 파일 필드, 파일 수 초과 | 422 각 입력 코드 |

GET /v1/jobs/{job_id}는 소유자만 상태·시도 횟수·결과 요약을 조회한다. 타인 소유와 없는
작업은 모두 404 job_not_found다.
