# Ingestion 1단계 계약

보관된 Go-first ingestion 설계의 최소 처리 계약 기록이다. 현재 Python 최소 runtime은 이 함수·DDL·dispatcher를 실행하거나 소유하지 않는다.
[전체 사용자 API](service-api-v1.md)의 초안 revision·index 검증·삭제 fencing은 아직 이 함수 계약에 구현되지 않았다.
공개 job succeeded/자료 활성화와 기존 최소 문서 처리 성공을 혼동하지 않는다. [연동 차이](implementation-gap-v1.md)를 기준으로 후속 합의한다.

Gateway가 schema 마이그레이션과 작업 상태 전이를 소유한다. ingestion은 별도 마이그레이션으로
이 테이블을 변경하지 않고 ingestion_load_attempt_input,
ingestion_store_attempt_success, ingestion_store_attempt_failure 함수만 사용한다.
DATABASE_URL, PERSONA_JOB_ID, PERSONA_ATTEMPT_ID로 인수 없는 entrypoint를 시작한다.

| 대상 | Gateway | dispatcher | ingestion |
| --- | --- | --- | --- |
| persona·원문·작업 생성 | 쓰기 | 읽기 | 없음 |
| 슬롯·시도·버전 상태 | 없음 | CAS 전이만 | 없음 |
| attempt_documents | 없음 | 성공 시 승격 | 현재 시도에만 기록 |
| ingestion_attempt_results | 읽기 | 읽기 | 현재 시도에 한 행 기록 |
| processed_documents | 읽기 | 현재 시도만 승격 | 직접 쓰기 없음 |

platform은 persona_ingestion 역할을 Gateway migration 전에 생성해야 한다. 역할이 없으면
Gateway migration은 실패하며, 권한 없는 배포 상태로 진행하지 않는다. ingestion DB 계정에는
원본/작업 상태 테이블의 직접 권한을 주지 않는다.
Gateway migration은 PUBLIC의 함수 실행 권한을 revoke하고 persona_ingestion role에만
ingestion_load_attempt_input, ingestion_store_attempt_success,
ingestion_store_attempt_failure 실행 권한을 준다. 함수는 현재 dispatching/running이고
current_attempt_id가 일치하는 시도만 입력을 읽고 임시 결과를 기록하도록 검증한다.

성공 함수의 documents JSON 배열은 id, source_id, ordinal, cleaned_content, sha256을 가진다.
성공 결과는 비어 있지 않아야 한다. 문서 수는 processed_document_count와 같아야 하며, source는 작업 version에 속하고
cleaned_content의 SHA-256이 sha256과 일치해야 한다. 같은 성공 저장은 동일 결과에만
멱등이고, 다른 재호출은 오류다.

worker는 성공 시 outcome success 결과와 문서 수를 기록한 뒤 exit 0으로 끝낸다. 실패 시
결과를 기록한 뒤 10=transient, 20=permanent, 21=configuration, 22=code로 종료한다.
OOM은 worker enum이 아니며 dispatcher가 Pod 상태에서만 분류한다.

| Kubernetes Job | worker 결과 | dispatcher 처리 |
| --- | --- | ---|
| Succeeded | success | 현재 시도 산출물 승격 후 성공 |
| Failed | transient + exit 10 | 최대 1회 재시도 |
| Failed | permanent/configuration/code와 일치하는 exit | 실패 |
| Failed | 결과 없음 + OOMKilled/Evicted | 실패 |
| 나머지 조합 또는 UID/Pod 동일성 불명 | reconciliation_required, 슬롯 유지 |

재시도 시 산출물은 attempt_documents에 attempt ID로 격리한다. dispatcher만 성공 시 이를 정식
processed_documents로 옮기므로 이전 시도가 새 결과·상태를 덮을 수 없다. 성공은 후보 자료가
최소 처리됐다는 뜻일 뿐 활성화·채팅 준비 완료가 아니다.
