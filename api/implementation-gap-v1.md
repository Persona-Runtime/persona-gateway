# 전체 API 계약과 현재 구현의 차이

확인일: 2026-09-10. 읽은 대상은 이 로컬 working tree이며 미커밋·untracked 코드가 포함돼 있다.
과거 테스트 통과나 Git 원격 배포 완료를 이번 구현 증거로 쓰지 않는다. 이번 작업은 Python 최소 runtime과
전용 `persona_minimal` 스키마 migration을 추가했으며, 실제 클러스터 배포는 하지 않았다.

## 확인된 구현

기존 Go Gateway·dispatcher·DDL은 검증된 레포 밖 로컬 보관본으로 전환했고 활성 런타임이 아니다.
`python-backend/`는 정적 Bearer 인증의 `GET /v1/me`, `GET /v1/personas`, `POST /v1/personas`를
구현한다. Python 경로는 FastAPI·psycopg·Alembic과 독립 `persona_minimal` schema를 사용한다.
새 명세 전체는 22 operations다.

| 차이 | 필요한 작업 |
| --- | --- |
| 상세·초안·대화·삭제·상태 route 없음 | `/me`, 목록, 생성 외의 19 operations는 아직 미구현이다. |
| 원문 접수는 여러 version 생성 가능 | 캐릭터당 단일 초안·입력 revision·적용 포인터·동시 접수 방지 필요 |
| 업로드·초안·삭제의 Idempotency-Key 지원이 명세 수준에 없음 | Python 생성은 사용자 행 잠금, DB fingerprint·replay로 구현했다. 나머지 작업의 멱등성과 tombstone 경계는 후속 작업이다. |
| Job 응답에 draft_revision/persona_id/can_retry 등 없음 | 외부 DTO와 내부 상태를 분리. nullable failure/result, 안전한 summary 검증 |
| 최소 처리 성공과 index 준비 완료가 다름 | 기존 DB 성공 함수의 비어 있지 않은 문서 검증은 유지. profile-only는 문서 1개 이상, index 0개 허용 |
| material_versions 상태에 draft/active bundle 개념 없음 | migration 순서·현재 데이터 이행·불변 snapshot/index 참조 설계 |
| 사용자 retry가 새 job을 만드는 규칙 미구현 | 자동 attempt retry와 구분, 옛 revision/실행과의 경합 차단 |
| 설정 수정 없이 전체 입력만 접수 | draft PATCH·source 편집/제거·설정 변경 동기화·revision CAS 추가 |
| 채팅 초안만 있고 SSE public route 없음 | generation 저장·동시성·취소/완료 경합·mock adapter·스트림/JSON replay 구분 |
| 캐릭터 삭제 실행/권한 미구현 | deleting fencing·Job 종료 확인·Qdrant/DB cleanup·실패 복구·삭제 기록 유지 |

## 소비자별 인수인계

- **gateway:** 공개 OpenAPI/DTO·소유권·작업·version·generation·deletion 마이그레이션 및 상태의 단일 소유자.
- **ingestion:** 확정된 job/revision/attempt snapshot을 읽어 정제·CPU 임베딩·index staging 결과를 기록. DDL 복제 금지.
  삭제 차단과 profile-only 결과 계약을 포함해 DB 함수 계약을 gateway와 함께 갱신한다.
- **web:** 4화면·토큰 입력, API별 오류, revision 재조회, 같은 키 재전송, SSE UTF-8 파싱과 JSON replay 분기.
- **platform:** 실제 migration/Secret/Job template/RBAC·삭제 제어 권한·배포·운영 timeout. CP에서 직접 운영하고 Argo로 배포.
- **ops-lab:** 합성 정상/실패/경쟁 fixture, 실 Job/DB E2E, 스트림 취소·파드 종료·기록 복구 검증. mock 응답을 LLM 품질 증거로 사용하지 않음.

## 먼저 구현할 묶음

현재 첫 묶음의 구체 범위·레포별 완료 조건은 [첫 사용자 흐름](../../docs/first-user-flow-plan.md)이다.
`draft.2`의 제한은 목표 계약이다. 실제 코드에 캐릭터 3개·원문 quota·사용자 단위 생성 슬롯·시간 제한·정리 정책이 구현됐다는 뜻이 아니다.

1. 단일 계정 인증·캐릭터 생성/목록과 JSON casing 정합성은 Python 최소 backend에서 구현·격리 Postgres로 검증했다. 상세 조회는 남아 있다.
2. 업로드·작업·초안 DTO와 migration/멱등/revision 경계 합의, ingestion 공통 계약 테스트.
3. 실제 처리·검색·활성화 연결 후 profile-only/수정/실패 유지 E2E.
4. 대화 저장·생성 adapter·SSE/취소/재시도와 frontend 실제 API 연결.
5. 삭제 상태·실행 정리·보존 정책·운영자 복구 검증.

첫 묶음에서 캐릭터 수의 사용자 범위 원자 검사·멱등 생성·응답 DTO와 실제 Postgres 검증을 추가한다.
나머지 원문 quota(공유 참조/사용량 응답 포함), 질문 길이·출력 토큰·사용자 단위 슬롯·시간 초과·보존/삭제는 각 기능 구현에서 별도로 검증한다.
명세 11절의 잔여 미결 값을 임의로 정하지 않는다. 현재 코드의 사용자 변경은 보존한다.
