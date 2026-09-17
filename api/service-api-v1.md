# Persona Runtime — 전체 사용자 API 계약 v1

작성일: 2026-09-10. 상태: **합의한 사용자 흐름·초기 제한의 계약 초안 `1.0.0-draft.3`**.
2026-09-16: 표시용 프로필 사진을 범위에서 **제외했다**(tradeoff/14). 아바타는 이름 기반 이니셜로 표시한다.
2026-09-16: `CreateDraft`에 `settings` 경로를 더해 **처리 없이 초안을 시작**할 수 있게 했다(5절).
operation과 path 수는 그대로다.
기계 판독 명세는 [openapi.json](openapi.json), 구현과의 차이는 [implementation-gap-v1.md](implementation-gap-v1.md).
실제 서버가 이 명세를 전부 지원한다는 뜻이 아니다. 기존 업로드 계약은 현재 구현 참고용으로 남긴다.
요청/응답 구조는 OpenAPI, 상태·경쟁·재시도·SSE 동작은 이 문서가 함께 정의한다. 충돌하면 구현을 진행하기 전에 두 문서를 같이 수정한다.
확정한 초기 제한·보존 정책과 남은 미결 값은 11절에 구분했다. 문서 반영은 자동 삭제나 런타임 구현 완료를 뜻하지 않는다.

## 1. 공통 계약

- 외부 API는 `/v1`, 사용자 화면은 같은 origin을 사용한다. 실제 토큰 전송 경로는 HTTPS/Tailnet이다.
- 단일 계정의 사전 발급 정적 Bearer 토큰을 검증한다. `GET /v1/me`는 검증/사용자 조회이지 토큰 발급 API가 아니다.
- 회원가입·비밀번호 찾기·토큰 발급·OIDC 전환은 이번 범위 밖이다. 사용자 ID는 서버의 토큰 매핑으로 결정한다.
- 토큰은 브라우저 메모리에만 보관한다. 새로고침·탭 종료 후 다시 입력한다. localStorage/sessionStorage/IndexedDB·영구 쿠키에 저장하거나 빌드에 삽입하지 않는다. URL·로그·분석 이벤트에도 넣지 않는다.
- UI 로그아웃은 메모리의 토큰·사용자 자료를 지우는 동작이며 서버 토큰 폐기는 아니다. 메모리 보관도 실행 중 XSS에 대한 방어를 대체하지 않는다. 만료·회전 절차는 별도 미결이다.
- 인증 없음/불일치는 401. 리소스 소유권은 매 요청 검증하며 타인 소유와 부재는 같은 404로 처리한다. 상태 API도 인증 대상이다.
- UUID 식별자, UTC RFC3339 날짜, JSON의 `snake_case` 사용. JSON 필드명/타입은 OpenAPI가 기준이다.
- 응답과 원문·대화에는 `Cache-Control: no-store`. CORS wildcard와 비밀값을 포함한 오류 응답은 사용하지 않는다.
- 원문·질문·참고 본문·답변은 서비스의 private 저장 대상이며 관측 로그/메트릭/트레이스에는 넣지 않는다.
- 목록은 opaque cursor 기반이며 기본 20/최대 100이다. 캐릭터·대화는 최신순, 메시지는 오래된순 `(created_at,id)`으로 고정한다.
  cursor는 소유자·부모 리소스·정렬과 일치해야 한다. 전체 개수 반환과 무한 크기 응답은 제공하지 않는다.
- `healthz/readyz`는 내부 배포 probe이며 이 사용자 API에 포함하지 않는다.

공통 오류 예시:

```json
{
  "error": {
    "code": "draft_revision_conflict",
    "message": "초안이 변경됐습니다. 다시 조회해주세요.",
    "request_id": "00000000-0000-4000-8000-000000000099",
    "fields": [{"field": "expected_revision", "code": "stale_revision"}]
  }
}
```

HTTP 오류: 400 문법/식별자/cursor, 401 인증, 404 소유권/부재, 409 상태·revision·중복 키 충돌,
413 바이트 상한, 415 형식, 422 의미 검증, 429 요청 제한, 503 의존 서비스 불가, 500 안전하게 축약한 내부 오류.
오류 `code`로 UI가 분기하고 `message` 문자열을 파싱하지 않는다. 사용자 입력이 포함될 수 있는 raw exception은 전달하지 않는다.

## 2. 객체와 상태의 경계

| 객체 | 의미 |
| --- | --- |
| persona | 소유자와 캐릭터의 고정 식별자. 목록 이름은 초기 이름 또는 마지막 적용 이름 |
| draft | 캐릭터당 최대 하나의 미적용 초안. 후보 `version_id`와 단조 증가 `revision` 보유 |
| version | 적용하면 불변인 설정·자료 묶음. 설정과 검증된 검색 결과 참조를 함께 고정 |
| job | 특정 초안 revision을 처리하는 작업. 자동 시도는 최대 2회 |
| generation | 질문 하나에 대한 응답 생성 시도. 재시도할 때 새 ID |
| deletion | 삭제 접수 이후 남는 소유자 범위의 최소 상태 기록 |

persona 표시 상태는 서버가 계산한다. `deleting`이 우선이며, 적용본이 있으면 초안 처리 여부와 무관하게 `ready`다.
적용본이 없으면 초안 없음=`needs_material`, 실행 중=`preparing`, 그 외 초안 존재=`review_required`다.
세부 실패·대기는 `draft.status`와 job으로 구분한다. `ready`는 GPU가 지금 켜져 있다는 뜻이 아니다.

job은 기존 `queued / dispatching / running / retry_wait / succeeded / failed / reconciliation_required`를 유지한다.
`dispatching`은 화면에서 처리 중, `reconciliation_required`는 상태 확인 중으로 표시한다. 상태 확인 중에는 새 실행을 시작하지 않는다.

draft는 `editing / processing / ready / failed`다. 수정 시 revision이 증가하고 해당 revision의 검증 상태를 다시 계산한다.
설정만 수정하고 기존 검색 결과를 안전하게 재사용할 수 있으면 바로 `ready`가 될 수 있다. `can_activate`는 서버 판정값이다.
처리 요청은 `expected_revision`을 고정해 job에 기록한다. 자동 상태 변화 자체는 사용자의 내용 revision을 증가시키지 않는다.

## 3. API 목록

| Method | 경로 | 성공 | 핵심 제약 |
| --- | --- | --- | --- |
| GET | `/v1/me` | 200 User | 인증된 고정 사용자 |
| POST | `/v1/personas` | 201 Persona | 비공백 이름, 사용자 내 이름 유일, 삭제 중 포함 최대 3개 |
| GET | `/v1/personas` | 200 PersonaPage | 삭제 중 포함, 삭제 완료 제외 |
| GET | `/v1/personas/{persona_id}` | 200 PersonaDetail | 현재 적용 설정·문서 요약·초안 요약 |
| POST | `/v1/personas/{persona_id}/uploads` | 202 JobAccepted | 최초 입력용. 적용본/초안 없어야 함 |
| GET | `/v1/jobs/{job_id}` | 200 Job | 소유자만 조회 |
| POST | `/v1/jobs/{job_id}/retry` | 202 JobAccepted | 실패 종료·동일 초안 revision·can_retry |
| POST | `/v1/personas/{persona_id}/draft` | 201 Draft | 현재 `base_version_id`에서 생성, 기존 초안 없음 |
| GET | `/v1/personas/{persona_id}/draft` | 200 Draft | 설정·원문·주의사항과 revision 조회 |
| PATCH | `/v1/personas/{persona_id}/draft` | 200 Draft | expected_revision, 실행 중 수정 금지 |
| DELETE | `/v1/personas/{persona_id}/draft` | 204 | query expected_revision, 실행 종료 확인 |
| POST | `/v1/personas/{persona_id}/draft/process` | 202 JobAccepted | 변경 자료 처리, 실행 중 중복 금지 |
| POST | `/v1/personas/{persona_id}/draft/activate` | 200 Activated | 최신 검증 revision만 원자 전환 |
| POST | `/v1/personas/{persona_id}/conversations` | 201 Conversation | 적용본 필요, GPU 가용성은 생성 조건 아님 |
| GET | `/v1/personas/{persona_id}/conversations` | 200 ConversationPage | 캐릭터별 소유자 목록 |
| GET | `/v1/conversations/{conversation_id}/messages` | 200 MessagePage | 질문별 생성 시도·저장된 부분 답변 |
| POST | `/v1/chat/completions` | 200 SSE 또는 replay JSON | conversation_id + message, 사용자당 활성 생성 1개 |
| POST | `/v1/generations/{generation_id}/cancel` | 200 Generation | 취소 접수와 종료 구분 |
| POST | `/v1/generations/{generation_id}/retry` | 200 SSE 또는 replay JSON | 최신 질문·최신 실패/중단 시도만 |
| DELETE | `/v1/personas/{persona_id}` | 202 DeletionAccepted | 삭제 잠금·작업 중단·비동기 정리 |
| GET | `/v1/deletions/{deletion_id}` | 200 Deletion | 삭제 후에도 소유자 조회 가능 |
| GET | `/v1/service-status` | 200 ServiceStatus | 안내용 snapshot, 처리 시 다시 확인 |
| GET | `/v1/personas/{persona_id}/retrieve` | 200 RetrieveResult | 디버그 전용 — `PERSONA_RETRIEVE_DEBUG_ENABLED`(기본 false) 꺼지면 404. q 1~2000자, k 1~10(기본 5) |

25 operations / 17 paths. 표의 타입 정의·필수 필드·nullable 값은 OpenAPI에 있다.
이전 설계에서 추가로 논의하지 않은 대화 삭제·응답 편집·정상 응답 재생성·실행 중 ingestion 사용자 취소 API는 만들지 않는다.

## 4. 접수와 중복 방지

모든 변경 API는 `Idempotency-Key` UUID를 요구한다. read API에는 불필요하다.
키는 `(owner, operation, target, key)` 범위로 보관하고 입력 fingerprint와 응답/대상 ID를 함께 기록한다.
키 생성·바인딩과 리소스 생성은 원자적으로 다룬다. 동시 동일 키에서 물리 실행이 중복될 수 있음을 고려해 DB 유일성·상태 대조가 필요하다.

1. 인증·소유권 및 삭제 차단 확인.
2. 기존 키 조회: 같은 입력이면 이미 접수한 ID/응답 재사용, 다른 입력이면 409 `idempotency_conflict`.
3. 처리 중인 키에 아직 리소스 ID가 없으면 409 `request_in_progress`; 같은 키로 나중에 재조회/재전송.
4. 신규 키일 때 초안/작업/revision 조건 확인 후 접수.

활성화 성공 뒤 초안이 없어져도 동일 키 성공 결과는 재전송할 수 있어야 한다. 삭제 완료 리소스도 소유자 범위의 tombstone으로 식별한다.
삭제 중/완료된 캐릭터의 과거 업로드·생성 키를 재전송해 새 작업을 살리거나 삭제된 텍스트를 반환하면 안 된다.
캐릭터 삭제 접수는 다른 키로 반복돼도 같은 deletion ID를 반환한다. 초안 폐기 동일 키는 204 replay다.
일반 접수는 원래 성공 상태 코드/응답을 replay하고 현재 상태는 GET으로 확인한다. 생성 replay만 200 JSON으로 최신 Generation을 반환한다.

multipart fingerprint는 임의 boundary 바이트가 아니라 입력 항목별 순서·파일명·원문 SHA를 기준으로 정규화한다.
API 의미에 영향 없는 JSON key 순서는 제외하고, 반복 파일·텍스트의 의미 있는 순서는 유지한다.
대상 작업/리소스가 남아 있는 동안 키를 유지해 조기 만료에 따른 중복 생성을 막는다.
삭제 이후에는 원문 없는 fingerprint/최소 식별자만 남긴다. 삭제 기록은 완료 후 7일이며 키의 삭제 이후 최종 정리 시점은 11절의 잔여 설계 항목이다.

## 5. 입력과 초안 수정

최초 `/uploads`는 기존 multipart 규칙을 유지한다. 텍스트 part는 profile/events/relationships/abilities/speech_examples,
파일 part는 반복 가능한 `files.<kind>`다. 범용 files, URL 수집, PDF/HWP/자막 입력은 지원하지 않는다.
UTF-8, 비공백 필수 profile, 파일당 1 MiB, 전체 원문 5 MiB, 파일 총 20개, multipart 6 MiB 상한을 사용한다.
20개/6 MiB는 기존 구현값을 계약 초안에 보존한 것이다. 파일 MIME만으로 본문·확장자 검증을 생략하지 않는다.
NUL·잘못된 UTF-8·알 수 없는 필드·동일 ID의 상충 수정은 거부한다. 파일명은 표시용 basename이며 경로나 명령으로 사용하지 않는다.
최종 초안에서도 비공백 profile은 필수다. 삭제/편집으로 이를 없애면 422이며 이름 중복은 편집 검증과 실제 적용 시점 모두 확인한다.

첫 접수 예시(텍스트 part):

```text
profile = 합성 캐릭터 모루. 침착한 도서관 안내자다.
events = 개관 첫날 분실된 지도책을 찾아냈다.
speech_examples = 길을 묻는 사람에게: 차근차근 같이 찾아볼까요?
```

```json
{"job_id":"00000000-0000-4000-8000-000000000010","version_id":"00000000-0000-4000-8000-000000000002","draft_revision":1,"status":"queued"}
```

초안을 만드는 경로는 둘이다. `POST draft`의 본문이 어느 쪽인지로 갈린다.

| 본문 | 뜻 | 쓰는 때 |
| --- | --- | --- |
| `{settings}` | **처리 없이 새 초안을 시작한다.** job을 만들지 않는다 | 적용본이 없는 캐릭터에 설정부터 저장할 때 |
| `{base_version_id}` | 적용본에서 파생한다 | 이미 적용된 캐릭터를 고칠 때 |

둘을 함께 보내면 거부한다. `{settings}` 경로가 `profile`을 요구하는 이유는
최종 초안에서도 비공백 profile이 필수이기 때문이다 — 빈 초안을 만들어 두고 나중에 채우게 하면
그 사이의 초안이 계약을 어긴다. job이 없으므로 이때 캐릭터 상태는 2절대로 `review_required`다.

`/uploads`는 여러 자료를 한 번에 접수하며 **처리 job을 함께 큐에 넣는** 경로다.
초안이 이미 있으면 `/uploads`를 재호출하지 않는다. 현재 초안 입력을 바꾸고 process를 호출한다.
기존 적용본 수정은 `POST draft(base_version_id)` → PATCH → 필요한 경우 process → activate다.

PATCH는 JSON이다. `expected_revision`과 선택적인 `settings`, `upsert_sources`, `remove_source_ids`를 받는다.
기존 문서는 id로 갱신하고, 신규 문서는 id 없이 보내 서버가 ID를 발급한다. 제거 ID는 현재 초안의 것이어야 한다.
모두 비어 있는 변경은 422다. 수정과 제거가 같은 ID를 가리키거나 서버/다른 초안 ID를 입력하면 거부한다.
파일 추가 시 웹이 UTF-8 텍스트로 읽어 kind/filename/content를 전송한다. 서버는 파일 출처 자료의 1 MiB 상한과 초안 전체 5 MiB 상한을 다시 검사한다.
PATCH의 JSON 포장은 제어문 escape로 늘 수 있으므로 multipart의 6 MiB를 그대로 적용하지 않는다.
JSON HTTP body의 제안 상한은 32 MiB이며 최종 확정 전까지 배포하지 않는다. 서버는 파싱 후 원문 상한도 별도로 적용한다.

```json
{
  "expected_revision": 3,
  "settings": {"profile": "합성 캐릭터 모루. 침착하며 모르는 사실은 모른다고 말한다."},
  "upsert_sources": [{"kind":"events","filename":"events.md","content":"개관 첫날 지도책을 찾아냈다."}],
  "remove_source_ids": []
}
```

설정 초안은 AI 자동 요약이 아니다. 최초 profile/speech_examples part들을 입력 순서대로 결합해 편집 가능한 설정 필드를 준비한다.
원문과 설정을 서로 다른 두 진실로 관리하지 않도록, 설정 수정 시 해당 종류의 현재 초안 텍스트도 함께 갱신한다.
동일 요청에서 `settings.profile`과 profile source, 또는 `settings.speech_examples`와 speech source를 동시에 수정하면 422 `conflicting_fields`다.
웹은 모든 본문을 텍스트로 표시하며 업로드 HTML/스크립트를 실행하지 않는다.


검색 대상은 events/relationships/abilities다. profile/speech_examples는 설정·말투 입력이며 index 필수 대상이 아니다.
profile만 있는 작업도 유효하다. 처리 문서는 비어 있지 않되 `indexed_chunk_count=0`일 수 있다.
검색 자료가 있었는데 파싱 실패로 모두 사라진 경우를 “정상 0건”으로 숨기지 않는다. 실패/주의사항을 구분한다.
설정 프롬프트가 모델 예산을 넘으면 확인 화면에서 수정하도록 안내한다. 자동 요약·무음 잘라내기를 가정하지 않는다.

## 6. 처리·재처리·활성화의 경쟁

- 사용자 내용 revision은 수정 시 증가한다. job은 입력 snapshot과 그 revision을 고정해 읽는다.
- 실행/재시도 대기/상태 확인 중인 초안은 수정·폐기·재처리·적용을 차단한다.
- 자동 재시도: 같은 job 안에 최초 포함 최대 2 attempt. 기존의 종료 증거/exit 10 분류 원칙 유지.
- 사용자 retry: can_retry=true인 종료된 failed job만. 같은 draft/version/revision에 새 job_id, retry_of_job_id 기록.
- 초안이 수정됐거나 이미 새 작업이 있으면 옛 job retry는 409. 입력 수정 후에는 draft/process를 사용한다.
- can_retry는 입력 불량·OOM·코드/설정 오류에 무조건 true가 아니다. 같은 조건의 무의미한 반복 대신 수정/운영자 조치를 안내한다.
- job 성공은 최신 revision 결과가 검증됐다는 뜻이지 자동 적용이 아니다. 처리 실패 시 기존 적용본 유지.

설정만 바꿨을 때 재임베딩하지 않으려면 **설정 version과 검증된 검색 결과 참조를 분리**해야 한다.
적용 version은 immutable settings + immutable index reference의 묶음이다. Gateway는 적용본 포인터를 한 번 읽어 두 값 모두 고정한다.
Qdrant 필터는 owner/persona와 그 version이 참조하는 index ID를 사용한다. 새 version ID로 무조건 필터해 기존 index가 조회되지 않는 결함을 피한다.
index가 없을 수 있는 profile-only 구성은 검색을 생략한다. 공유 index는 이를 참조하는 적용본/진행 중 요청이 있는 동안 삭제하지 않는다.

materialization은 job/attempt/revision으로 격리하고 dispatcher가 최신 결과만 publish한다.
Postgres+Qdrant의 원자 트랜잭션을 가정하지 않는다. 검증된 결과만 참조한 뒤 활성 포인터를 CAS로 전환한다.
`activate`는 expected_revision, base version, can_activate, 삭제 여부를 확인한다. 성공 후 초안 슬롯은 비워 다음 수정을 허용한다.
이전 답변이 사용하는 version/input snapshot은 종료되기 전에 정리하지 않는다.

## 7. 대화·질문·재시도

새 대화는 적용본이 있어야 한다. GPU가 꺼져도 빈 대화와 기존 기록 조회는 가능하다.
대화 목록 title은 최초 질문의 짧은 표시용 일부 등 결정적 방식으로 만들며 LLM 제목 생성은 하지 않는다. 구체 길이는 UI 규칙으로 정한다.
`material_changed`는 initial_version_id와 현재 적용본이 다른지 표시한다. 새 대화 권장 안내일 뿐 기존 기록을 변경하지 않는다.
메시지 조회는 질문마다 user_message와 generations 배열을 반환한다. 중단·실패 기록이 완성 답변처럼 섞이지 않는다.

질문 예시:

```json
{"conversation_id":"00000000-0000-4000-8000-000000000003","message":"모루야, 개관 첫날 무슨 일이 있었어?"}
```

일반 새 질문은 서버가 현재 적용 version을 선택한다. 사용자 ID·모델명·임의 version·Job 설정을 요청으로 받지 않는다.
사용자당 queued/running/cancel_requested/reconciling 생성은 모든 캐릭터·대화를 합쳐 최대 하나이며 UI 버튼 외에 DB에서도 강제한다. 대기열은 없다.
여기서 queued는 접수 후 실행 준비 중인 단일 생성이지 다음 요청의 대기열이 아니다. 재시도에도 같은 제한을 적용한다.
다른 대화에 활성 생성이 있으면 409 `generation_in_progress`로 거절한다. 동일 키 replay는 새 슬롯을 차지하지 않는다.
삭제 중/활성 생성 존재/사용 불가 backend는 스트림 시작 전에 409 또는 503으로 거절한다.
사전 거절된 질문을 성공적으로 접수한 메시지로 저장하지 않는다. 접수 뒤 실패하면 생성 기록은 남긴다.
질문·생성 ID와 고정 version·재시도에 필요한 input snapshot을 내구 저장한 뒤 스트림을 시작한다.
과거 대화는 예산 안의 최근 정상 완료 턴만 모델에 전달한다. 중단 답변은 기본 제외하고 자동 요약은 하지 않는다.

generation 상태: queued → running → completed/failed, 중단 요청 시 cancel_requested → cancelled.
종료 증거가 불명확하면 reconciling으로 유지하며 같은 사용자의 새 생성 요청을 받지 않는다. 제한 시간 경과나 HTTP 연결 종료만으로 슬롯을 해제하지 않는다.
완료와 취소가 경합하면 먼저 확정된 terminal 상태를 유지한다. 200 cancel 응답만으로 취소 완료를 주장하지 않는다.
서버 저장과 클라이언트 화면은 별개다. 완료를 DB에 저장한 뒤 done을 전송한다. done 전달 실패가 완료 기록을 취소로 바꾸면 안 된다.
부분 답변은 저장된 범위까지만 재조회 가능하다. 스트림으로 보인 모든 바이트의 내구 저장을 보장한다고 표현하지 않는다.

재시도는 대화의 최신 질문·최신 실패/중단 시도만 허용한다. 이후 질문이 있으면 409 `retry_not_latest`로 새 질문을 안내한다.
같은 user_message_id에 새 generation/assistant_message ID를 연결하고 원래 version·입력 snapshot을 사용한다.
입력 snapshot/버전이 제거됐거나 캐릭터가 삭제 중이면 거절한다. 현재 version으로 조용히 바꾸지 않는다.
이전 실패 기록은 유지하되 후속 모델 문맥에는 선택된 정상 완료 시도만 한 번 포함한다.

## 8. SSE 계약

브라우저는 Authorization 헤더와 POST body를 보내고 fetch의 응답 stream을 읽는 방식으로 구현한다.
연결을 자동 재생성하는 EventSource 사용이나 Last-Event-ID resume를 이번 API의 계약으로 가정하지 않는다.
SSE는 UTF-8 text이며 network read 한 번이 이벤트 하나라고 가정하면 안 된다. 빈 줄 경계로 조립하고 분할된 UTF-8도 처리한다.
참고: [MDN SSE 형식](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events).

정상 순서: meta 1회 → citations 1회(없으면 빈 배열) → delta 0회 이상 → done 1회.
meta 이후 실패 시 error로 종료할 수 있고 done은 보내지 않는다. 갑작스러운 단절은 terminal 이벤트가 없을 수 있다.
delta index는 생성별 0부터 단조 증가한다. comment heartbeat는 선택 가능하며 답변 조각으로 세지 않는다.
정상/재시도 첫 접수는 `text/event-stream`; 동일 키 재전송은 `application/json`의 `{replayed:true,generation:{...}}`다.
클라이언트는 Content-Type을 확인한다. replay JSON을 SSE로 파싱하거나 새 생성을 자동 시작하지 않는다.
취소·단절 이후 상태는 메시지 조회에서 generation_id로 확인한다. 살아 있는 생성의 텍스트를 이어받는 API는 없다.

OpenAPI의 SseMeta/SseCitations/SseDelta/SseDone/SseError가 각 data JSON의 스키마다.
`x-sse-events`는 우리 검증용 확장이며 일반 OpenAPI 코드 생성기가 이벤트 의미까지 구현해주지는 않는다.
HTTP 헤더 전 실패는 JSON/HTTP 오류, 헤더 후 실패는 SSE error(가능할 때)와 저장 상태로 구분한다.
citations는 검색해 전달한 자료이지 모델이 실제 활용한 모든 근거의 증명은 아니다.
mock backend의 `chunk`는 Gateway에서 delta로 변환하고, 원본 mock done을 곧바로 사용자 done으로 전달하지 말고 저장 완료를 먼저 확인한다.
실제 운영 중 LLM이 꺼졌다고 몰래 mock으로 fallback하지 않는다. mock은 명시적 개발 mode다.

## 9. 삭제와 서비스 상태

캐릭터 삭제는 원자적으로 deleting 표시와 deletion record를 만든 뒤 202를 반환한다.
동시에 들어오는 새 질문·적용·업로드와 직렬화해, 삭제 수락 후 새 작업이 시작되지 않게 한다.
실행 중 Job/생성을 중단하고 종료 확인·늦은 쓰기 fencing 후 정리한다. 일반 사용자용 ingestion cancel API가 없다는 것과 모순되지 않는다.
ingestion 전용 역할/함수도 삭제 중 입력 읽기·늦은 결과 쓰기를 거부해야 한다.

삭제 대상은 서비스가 보유한 업로드·초안/버전·처리 산출물·벡터·질문/답변·재시도 입력 snapshot이다.
캐릭터 삭제는 논리 삭제(`deleted_at`)라 DB의 참조 연쇄로 지워지지 않는다. 삭제 worker가 명시적으로 지운다.
처리 도중 실패하면 deletion failed와 persona deleting을 유지한다. 반쯤 지워진 캐릭터를 다시 ready로 표시하지 않는다.
v1 복구는 운영자 런북으로 수행하며 사용자 복구 API/휴지통 UI는 없다. 삭제 worker 실행 방식·권한은 platform/gateway에서 별도 구현 설계한다.
성공 후 persona GET은 404, 삭제 상태는 최소 owner-scoped record로 계속 조회 가능하다. 이름·본문을 tombstone에 남기지 않는다.
백업·복제본·로컬 원천 파일까지 무조건 지웠다는 뜻은 아니다. 백업 정책이 미결이면 “모든 사본 영구 삭제”라고 표시하지 않는다.

service-status는 메타데이터 snapshot이다. generation.mode는 mock/llm, available은 그 시점의 사용 가능 판단이다.
backend 단절만으로 GPU가 꺼졌다고 단정하지 않는다. 일반 실패는 backend_unavailable, 확인 불가능하면 status_unknown을 쓴다.
ingestion 대기열에 작업이 있다는 것과 접수 불가능한 것은 다르다. enabled/의존성/접수 가능 여부를 판단한다.
오류 이유에 내부 IP·자격증명·원문 오류를 넣지 않는다. 이 API가 available=true였어도 각 요청에서 재검증한다.

## 10. 구현 인수 조건

1. 모든 작업의 소유권·idempotency·revision·삭제 경쟁을 실제 Postgres 통합 테스트로 검증.
2. 첫 profile-only 업로드 성공, 문서 추가/제거, 설정-only 재사용, 실패 후 옛 자료 유지, 적용 전 혼입 방지.
3. 같은 키 중복 업로드/재시도/적용, 응답 유실, 과거 작업 늦은 쓰기와 중복 활성 생성 차단.
4. SSE UTF-8/이벤트 분할, done/error/EOF 구분, 취소/완료 경합, replay JSON, 새로고침 기록 조회.
5. 캐릭터 삭제 중 처리 결과 도착·벡터 정리 실패·운영자 복구·과거 키 재전송을 테스트.
6. 합성 fixture로 웹 → 실제 intake/Job/DB/검색 → mock generation을 연결. 실제 LLM 품질은 GPU 연결 후 별도 검증.

## 11. 확정 정책과 남은 설계

### 초기 서비스 제한 — 사용자 승인

| 항목 | 확정값·처리 |
| --- | --- |
| 캐릭터 수 | 사용자당 최대 3개. 삭제 중도 포함하고 삭제 완료 후에만 슬롯 반환 |
| 원문 합계 | 사용자당 100 MiB(104857600 bytes). 적용본·초안·보관 중인 구버전의 원문 포함 |
| 제출 입력 | 파일당 1 MiB, 합계 5 MiB, 파일 20개, multipart body 6 MiB 유지 |
| 이번 질문 | 최대 2,000 Unicode 코드 포인트. 비공백 필수, 초과 시 422 `message_too_long`; 무음 잘라내기 없음 |
| 생성 답변 | 최대 512 모델 토큰. 글자 수가 아니며 엔진 토크나이저 기준 |
| 동시 생성 | 사용자당 1개, 대기열 없음. 종료 확인 전에는 취소·시간 초과도 슬롯 유지 |
| 첫 답변 대기 | 접수 후 최대 60초. meta/citations/heartbeat는 첫 답변 조각이 아님 |
| 전체 생성 | 같은 접수 시점부터 최대 180초. 첫 답변 뒤 타이머를 새로 시작하지 않음 |
| 토큰 보관 | 브라우저 메모리만. 새로고침·탭 종료 후 재입력 |

한도와 오류 코드는 구현 계약으로 서버에서 강제한다. 문자 수는 클라이언트 UTF-16 length와 서버의
Unicode 문자 수·byte length를 구분하며 한글·이모지 경계 테스트를 둔다.
캐릭터 한도 초과는 409 `persona_limit_exceeded`, 원문 한도 초과는 413 `storage_quota_exceeded`로 거절한다.
한도는 신규 증가 작업만 차단한다. 기존 조회·채팅·삭제를 막거나 보존 중인 데이터를 몰래 지우지 않는다.
동시 생성/업로드가 각각 검사만 통과해 한도를 넘지 않게 사용자 범위에서 검사와 반영을 원자 처리한다.
원문 사용량/남은 용량을 UI에 안내한다. 공유 원문 참조의 중복 계수·예약/해제 원자성·사용량 응답 필드는 업로드 구현 전 계약 보강 대상이다.
원문 quota는 DB·벡터·대화·WAL·관측 데이터를 포함한 실제 디스크 상한이 아니다. 각각 별도 용량 관측이 필요하다.

512토큰에 도달한 정상 답변은 `done.finish_reason=length`로 저장·표시한다. 시간 초과와 혼동하지 않는다.
첫 답변/전체 시간 초과는 각각 `first_response_timeout`/`generation_timeout`으로 안전하게 안내하고 생성 중단을 요청한다.
SSE 연결이 살아 있으면 error를 보내고 done은 보내지 않는다. 부분 답변은 중단 상태로 남기며 자동 재시도하지 않는다.
종료 확인 전 cancel_requested/reconciling을 유지하고, 종료 확인 후 실패 원인을 보존한 failed로 확정한다.
자료 조회·편집 등 비생성 작업에는 생성 슬롯을 적용하지 않는다. 처리 중 초안 수정 금지 등 기존 제약은 유지한다.
60/180초는 성능 목표가 아닌 무한 대기 방지값이다. 부하 실험에서는 서버 운영 설정으로 동시성·시간 제한을 조절하고 적용값을 기록한다. 사용자 요청으로 제한을 우회하지 않는다.
설정·검색 자료·이전 대화는 이번 질문 2,000자 한도 밖이며 별도의 전체 모델 문맥 예산을 적용해야 한다.
mock 출력에는 실제 모델 토큰 수를 꾸며내지 않는다. 512토큰 강제·length 종료의 실제 엔진 검증은 GPU 연결 후 수행한다.

### 보존과 삭제 — 사용자 승인

| 데이터 | 보존 |
| --- | --- |
| 적용 중 설정·원문·검색 자료 | 캐릭터 삭제까지 |
| 미적용 초안·실패 작업 재처리에 필요한 원문 | 적용 또는 명시적 폐기까지 |
| 대화·중단된 부분 답변 | 캐릭터 삭제까지 |
| 구버전·미사용 처리 산출물 | 사용 종료 후 7일 |
| 삭제 상태/결과 최소 기록 | 삭제 완료 후 7일, 이름·본문 제외 |
| 멱등 키 | 대상 작업/리소스가 남아 있는 동안 조기 만료 금지 |

실행 중 요청·적용본·초안·보존 중인 재시도 snapshot이 참조하는 자료는 정리하지 않는다. 참조가 해제되어 실제 사용이 끝난 시점부터 구버전 정리 기간을 계산한다.
캐릭터 삭제는 7일 유예가 아니다. 새 실행 차단 → 실행 종료 확인/늦은 쓰기 차단 → 서비스 원문·벡터·대화·임시 결과 정리 → 성공 기록 순서다.
미완료 삭제 기록은 7일이 지나도 버리지 않는다. 실패는 deleting/복구 필요로 유지하며 휴지통·복원 UI는 없다.
백업은 별도다. 배포 전 실제 pg_dump 등 사본과 보존 정책을 확인하고, 그 검증 없이 모든 사본 영구 삭제라고 표시하지 않는다.

### 아직 미결

- 백업 사본 삭제 범위·기한, 복원 시 삭제 기록 재적용, 삭제 이후 멱등 기록의 최종 정리 시점.
- 설정/검색/이전 대화의 모델 입력 토큰 예산, API rate limit, ingestion·종료 확인·reconciliation 제한과 운영자 복구 절차.
- 토큰 만료·회전 절차. 메모리 보관을 서버 토큰 자동 만료로 해석하지 않는다.
- PATCH JSON 32 MiB는 초안 제안값이며 별도 승인이 필요.
- 원문 quota 집계/사용량 응답 계약. 첫 캐릭터 생성·조회 묶음에는 원문 업로드를 넣지 않는다.

미결 항목은 그것이 필요한 기능의 구현·배포 전에 해결한다. 첫 로그인·목록·생성 구현을 위해 GPU나 삭제 전체 구현을 앞당기지 않는다.
OpenAPI는 [3.1.0 명세](https://spec.openapis.org/oas/v3.1.0.html)를 사용한다. 이 문서 작성은 런타임/DDL/클러스터 변경이 아니다.

## 12. 로컬 명세 검증

아래 검사는 개발 노트북에서 실행하며 홈서버·DB·사용자 API에 접속하지 않는다.
검증 의존성은 임시 uv 환경에만 설치되고 runtime 이미지에는 추가되지 않는다.

```sh
uv run --no-project --with openapi-spec-validator==0.9.0 --with jsonschema==4.26.0 python api/validate_contract.py
```

OpenAPI 구조·참조, operation 수/ID·인증·멱등 키 계약, `contract-examples.json`의 정상/오류 28개,
SSE data JSON 예시 8개, 영문·한글·이모지 질문 2000/2001자 경계 6개와 확정 정책값을 검증한다.
DB 상태 전이·소유권 경쟁·실제 스트리밍 타이밍·byte 상한·토큰 제한의 런타임 강제는 이 검사로 검증되지 않는다.
그 항목들은 10절의 HTTP/DB/E2E 테스트에서 별도로 확인한다.
