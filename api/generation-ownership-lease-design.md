# generation 소유권 lease (G-1)

상태(2026-09-25): **코드 구현과 로컬 검증까지 했다. 커밋·이미지·migration 적용·배포는 하지
않았다.** 이 브랜치는 배포 순서의 1단계인 **bridge 릴리스**다(3절). 아래 "이전 동작"은
develop(`b35c9b8`)까지의 동작이고, "적용된 동작"은 이 브랜치
(`feat/generation-ownership-lease`)의 동작이다. "미구현"은 아직 없는 것이다.

## 1. 이전 동작과 문제

Gateway가 기동하면 lifespan이 `reconcile_stale_generations_on_startup()`으로 **소유자를 구분하지
않고** `queued`·`running`·`cancel_requested` generation을 전부 `reconciling`으로 바꿨다. 롤링
배포(`maxSurge: 1`)에서는 새 Pod가 살아 있는 이전 Pod와 겹쳐 뜨므로, 이전 Pod에서 정상
스트리밍 중인 SSE가 매 배포마다 중단됐다.

- 클라이언트: `done` 대신 `error`(`code: "cancelled"`, `status: "failed"`)
- DB: `reconciling`, 같은 사용자는 300초 동안 `409 generation_in_progress`
- 지표: `finished_total{terminal_reason="reconciling"}` 증가, `stream_disconnects_total`은 그대로

replica를 2로 늘려도 롤링 중 새 Pod 기동은 계속 일어나므로 이 영향은 남는다. 그래서 G-1은
replica 2·PDB·분산 적용의 선행 조건이다.

## 2. 적용된 동작

### 2.1 저장 구조 — generation 행에 소유자·lease (별도 인스턴스 테이블 없음)

`generations`에 `owner_instance_id uuid`, `lease_expires_at timestamptz`를 추가했다
(migration `0005_generation_lease`, 둘 다 NULL 허용, 활성 상태 부분 인덱스 2개).

| 기준 | 선택: generation 행에 둔다 | 대안: `gateway_instances` 테이블 |
| --- | --- | --- |
| heartbeat 쓰기 | 인스턴스당 주기마다 UPDATE 1문장. 바뀌는 행 = 그 인스턴스가 스트리밍 중인 generation 수(사용자당 최대 1개) | 인스턴스당 주기마다 1행 |
| 회수 쿼리 | 단일 테이블 한 문장(`status` + `lease_expires_at < now()`) | 조인·서브쿼리와 죽은 인스턴스 행 정리가 필요 |
| 권한·배포 | 컬럼 추가라 기존 테이블 권한을 상속 — **grants 변경 없음** | 새 테이블이라 persona-platform grants 변경이 선행돼야 함 |
| 원자성 | 소유권과 만료 판정이 같은 행 | 두 테이블 사이 경쟁 고려 필요 |

쓰기량 차이는 활성 generation 수만큼이고 작다. 회수가 한 문장이고 배포에 grants 단계가 없다는
점을 근거로 골랐다.

### 2.2 인스턴스 ID와 lease

- 인스턴스 ID: 앱(프로세스)을 만들 때 `uuid4()`(`ChatStore.instance_id`). Pod 이름을 쓰지 않는다 —
  같은 이름의 컨테이너가 재시작돼도 이전 프로세스와 구분해야 하고, 이름만으로는 생존 여부를
  모른다. 로그·메트릭 label에 남기지 않는다.
- 소유권 기록: 접수 INSERT(`queued`), `running` 전환 UPDATE(전이가 실제로 일어난 경우), retry
  INSERT(`running`)와 **같은 트랜잭션에서** `owner_instance_id`와 `lease_expires_at = now() + lease`를
  쓴다(`ChatStore._stamp_owner`). 같은 트랜잭션으로 커밋되므로 전이와 소유권 기록은 원자적이다.
  lease 컬럼이 없는 0004 DB(bridge 기간)에서는 기록하지 않는다(3절).
- 연장: `chat/lease.py` `GenerationLeaseKeeper`의 heartbeat 스레드가 간격마다
  `UPDATE … SET lease_expires_at = now() + lease WHERE owner_instance_id = 나 AND 활성 RETURNING id, status`.
  모든 시각 판정은 DB `now()`로 한다.
- 설정과 관계:

  | 설정 | 기본값 | 규칙 |
  | --- | ---: | --- |
  | `PERSONA_GENERATION_HEARTBEAT_SECONDS` | 10 | 연장 간격 |
  | `PERSONA_GENERATION_LEASE_SECONDS` | 30 | `lease >= 2 × heartbeat + PERSONA_DB_TIMEOUT_SECONDS(2)`. 위반 시 기동 실패 |

  연장이 한 번 실패하고 DB가 timeout까지 느려도(10 + 10 + 2 < 30) 살아 있는 소유자의 lease는
  만료되지 않는다. 대가: 소유자가 비정상 종료하면 회수까지 최대 30초가 걸린다. uvicorn graceful
  shutdown(25초) 동안에도 heartbeat는 돌고, lifespan 종료에서 멈춘다.

### 2.3 회수 규칙

회수 조건은 한 곳(`_RECLAIMABLE_CONDITION`)에 있고 회수 UPDATE의 WHERE에 그대로 들어간다.

```
status IN ('queued','running','cancel_requested') AND (
  (owner_instance_id IS NOT NULL AND lease_expires_at < now())
  OR (owner_instance_id IS NULL AND heartbeat_at < now() - 240초)
)
```

- 판정과 전환이 한 문장이라, 두 인스턴스가 동시에 회수해도 Postgres가 행 잠금 뒤 조건을 다시
  평가해 같은 행은 한쪽만 바꾸고 메트릭도 한 번만 오른다.
- 회수 시 `heartbeat_at`은 **마지막 생존 확인 시각**으로 남긴다(소유자가 있으면 `lease_expires_at`,
  없으면 기존 값). reconciling의 300초 지연 해소는 이 시각부터 센다 — 회수 시각(now())을 쓰면
  이미 오래 방치된 행의 시계가 처음부터 다시 돈다.
- 회수는 **슬롯 해제가 아니다.** reconciling → `failed(reconciliation_timeout)`은 기존대로 같은
  사용자의 다음 요청이 300초 규칙으로 정한다.

회수가 일어나는 곳:

| 경로 | 호출 | 메트릭 reason |
| --- | --- | --- |
| 기동 | lifespan 시작 `reclaim_expired_generations()` — 이전의 전역 reconcile을 대체 | `startup_lease_expired` |
| 사용자 요청 | 접수·retry 직전 `reclaim_user_expired_generations(user)` — **별도 트랜잭션으로 먼저 커밋**(슬롯 검사의 409가 접수 트랜잭션을 ROLLBACK하므로) | `request_lease_expired` |
| 정상 종료 | lifespan 종료: heartbeat 정지 → `release_own_generations()`(자기 소유 활성 행만) | `shutdown` |

background sweep은 두지 않는다(기존 원칙). 모든 Pod가 살아 있을 때 죽은 소유자의 행은 그
사용자의 다음 요청에서 회수된다.

**요청 시 회수와 idempotency의 순서.** 접수·retry는 다음 순서를 지킨다.

1. 사용자 행을 잠근 짧은 트랜잭션에서 idempotency record만 본다 — replay면 기존 결과를 즉시
   돌려주고, 같은 키·다른 요청이면 즉시 409(`idempotency_conflict`). **여기서는 회수하지 않는다.**
2. 새 요청일 때만 만료 행을 별도 트랜잭션으로 회수·커밋한다.
3. 본 트랜잭션: 사용자 잠금 → idempotency 재확인 → 활성 슬롯 검사 → 삽입.

1을 회수보다 앞에 두는 이유는 "replay는 한도 검사보다 먼저"라는 계약이다 — 같은 키 재전송이
응답 전에 다른 활성 행을 바꾸면 안 된다. 회수를 3 안에 두지 않는 이유는 슬롯 검사의 409가
트랜잭션을 ROLLBACK해 회수도 사라지기 때문이다. 3에서 idempotency를 다시 보는 이유는 1과 3
사이에 같은 키의 다른 요청이 먼저 삽입했을 수 있어서다(그 경우 replay).

### 2.4 종료 유형별 상태 전이

| 상황 | DB 전이 | 클라이언트 | 지표 |
| --- | --- | --- | --- |
| 정상 완료 | running → completed | `done` | `finished{completed}` |
| 새 Pod 기동(이전 Pod 살아 있음) | **변화 없음**(lease 유효) → 이전 Pod가 completed로 마침 | `done` | reclaimed 0 |
| 브라우저 연결 종료 | running → reconciling(`now()`) | — | `stream_disconnects` +1 |
| 정상 종료(SIGTERM) | 25초 안에 끝나면 completed. 남은 스트림은 uvicorn이 닫아 연결 종료 경로로 reconciling. 그 뒤에도 남은 자기 행 → reconciling | 연결 끊김 | `stream_disconnects` 또는 `reclaimed{shutdown}` |
| OOM·노드 장애·SIGKILL | lease 만료 전: 변화 없음. 만료 뒤 기동 또는 그 사용자의 다음 요청 → reconciling(마지막 lease 시각) | 연결 끊김 | `reclaimed{startup_lease_expired\|request_lease_expired}` |
| 다른 인스턴스로 온 cancel | 받은 쪽이 cancel_requested. 소유자의 heartbeat가 읽어 로컬 업스트림 취소 → cancelled | `error(status=cancelled)`(최대 heartbeat 간격 지연) | `finished{cancelled}` |
| 다른 인스턴스로 온 retry | 새 generation은 retry를 받은 인스턴스가 소유 | 새 스트림 | — |
| 구버전(소유자 없음) 행 | 240초 지나면 회수 대상 | — | 회수 경로의 reason |

알려진 한계: 정상 종료에서 uvicorn이 강제로 닫은 스트림은 브라우저 연결 종료와 같은 경로를 타
`stream_disconnects`로 집계된다. lease가 만료됐는데 소유자가 실제로 살아 있던 경우(연장 실패가
lease보다 길게 이어짐)에는 회수 뒤 그 소유자의 `finish_generation`이 reconciling 행을 덮어쓰지
않는다(기존 WHERE 가드) — 결과를 모르는 상태로 남고 300초 규칙을 따른다.

### 2.5 관측

| 이름 | type | label |
| --- | --- | --- |
| `persona_chat_generations_reclaimed_total` | Counter | `reason`(startup_lease_expired·request_lease_expired·shutdown) |
| `persona_chat_lease_heartbeat_failures_total` | Counter | 없음 |

회수는 terminal이 아니므로 `generations_finished_total`에 넣지 않고, `stream_disconnects_total`과도
섞지 않는다. 연장 실패 로그에는 예외 타입만 남긴다.

## 3. migration·배포 순서 (아직 실행하지 않음)

"허용 revision만 넓힌 호환 이미지"는 안전하지 않다 — develop 코드에는 기동 시 전역 reconcile이
남아 있어, 그 이미지를 롤링하는 순간 1절의 스트림 중단이 migration 단계에서 재발한다. 그래서 첫
단계는 **lease 스키마를 인식하는 bridge 이미지**다.

**아래 순서는 필수다. 단계를 건너뛰거나 바꾸지 않는다.**

| 단계 | 이미지·작업 | `SUPPORTED_ALEMBIC_REVISIONS` | 비고 | 순서를 어기면 |
| --- | --- | --- | --- | --- |
| 1 | **bridge 릴리스 = 이 브랜치** | `("0004_chat", "0005_generation_lease")` | 전역 reconcile 없음. lease 컬럼 존재를 스스로 판정 | develop 이미지로 곧장 migration하면 전역 reconcile이 남아 롤링 중 스트림이 끊긴다 |
| 2 | migration Job `0005_generation_lease` | — | 컬럼·인덱스 추가. **grants 변경 없음** | — |
| 3 | 기능 릴리스(후속 한 줄 커밋) | `("0005_generation_lease",)` | 0005 적용·검증 뒤 좁히기. 판정 함수는 호환 창 도구로 남긴다 | 2보다 먼저 배포하면 새 Pod가 NotReady라 롤아웃이 멈춘다 |
| 4 | persona-platform: Gateway replica 2·PDB·worker 분산 | — | 구현하지 않음(ROLL-01B 전제) | G-1이 배포되기 전에 하면 새 Pod 기동마다 진행 중 스트림이 끊긴다 |

기능 릴리스의 diff(3단계, 아직 만들지 않음):

```diff
-SUPPORTED_ALEMBIC_REVISIONS = ("0004_chat", "0005_generation_lease")
+SUPPORTED_ALEMBIC_REVISIONS = ("0005_generation_lease",)
```

### bridge의 두 모드

lease 컬럼 판정(`repository.lease_schema_ready`)은 alembic 마커가 아니라 **시스템 카탈로그로 컬럼
실재**를 본다 — 쓰려는 것이 그 컬럼이고, 카탈로그 조회는 MVCC라 migration 중의 테이블 잠금을
기다리지 않는다. `owner_instance_id`·`lease_expires_at` **둘 다** 있을 때만 준비됐다고 본다 — 수동
DDL·부분 복구·스키마 drift로 하나만 남은 DB에서 lease 모드로 들어가면 없는 컬럼을 써서 500이 된다.
`ChatStore`는 한 번 True를 확인하면 고정하고, False인 동안은 매번 다시 본다 —
bridge가 떠 있는 중에 migration이 적용되면 **재시작 없이** 다음 요청부터 lease를 쓴다(PostgreSQL
DDL은 트랜잭션이라 컬럼은 migration 커밋 순간 한꺼번에 보인다).

| 동작 | 0004(컬럼 없음) | 0005(컬럼 있음) |
| --- | --- | --- |
| Ready | 200 | 200 |
| 소유자·lease 기록 | 하지 않음(INSERT는 이전과 같음) | 접수·running 전이·retry와 같은 트랜잭션에서 기록 |
| heartbeat | DB를 건드리지 않음, 실패 메트릭 0 | lease 연장 + cancel 전달 |
| 기동·요청 시 회수 | 소유자 없는 행 규칙만: `heartbeat_at` 240초 경과. **전역 reconcile 없음** | 만료 lease + 소유자 없는 행 규칙 |
| 정상 종료 반납 | 없음(내 행을 가릴 수 없음) → 240초 규칙으로 다른 인스턴스가 회수 | 자기 행 반납 |
| 다른 인스턴스로 온 cancel | 조기 전달 없음 — 스트림 끝에서 `finish_generation` CASE로 `cancelled` | heartbeat 간격 안에 전달 |
| 300초 지연 해소 | 그대로 | 그대로 |

0004 모드에서 살아 있는 스트림이 240초 규칙에 걸리지 않는 이유: 생성 전체 한도가 180초라 활성
행의 `heartbeat_at`(접수 또는 running 전이 시각)은 그보다 오래될 수 없다.

### 롤백 (이미지만, DB는 0005 유지)

이미지 롤백의 목적지는 bridge 이미지다(DB가 0005여도 Ready이고 lease를 인식한다). 그래서 기능 →
bridge 롤백 중에도 살아 있는 스트림은 끊기지 않는다. develop 이전 이미지(0004만 허용)는 0005 DB에서
Ready가 되지 않으므로 이미지 롤백의 목적지가 될 수 없다. DB까지 되돌려야 하는 경우는 아래 절을 따른다.

### 0005 downgrade 금지 조건

**lease-aware Gateway(bridge·기능 릴리스)가 하나라도 실행 중일 때 `0005_generation_lease`를
downgrade하지 않는다.** bridge는 확장 방향(0004 → 0005)을 운영 중에 안전하게 넘기는 장치이지, 역방향
DB 호환을 보장하는 장치가 아니다.

- 이유: `ChatStore`는 lease 컬럼을 한 번 확인하면 True로 고정한다(운영 중 downgrade는 지원 범위 밖이라
  의도한 설계다). 실행 중에 컬럼이 사라지면 접수의 소유자 기록·heartbeat 연장·회수 쿼리가 없는 컬럼을
  참조해 실패한다.
- 부득이하게 DB를 0004로 되돌려야 하면 **서비스 중단을 감수하는** 순서로 한다(사람이 수행):
  1. Gateway를 0개로 줄여 lease-aware 프로세스를 모두 멈춘다.
  2. `0005_generation_lease` downgrade Job을 실행한다.
  3. 0004를 허용하는 이미지로 다시 띄운다 — 새로 뜬 bridge는 0004 모드로 시작하고, develop 이전
     이미지도 가능하다(이 경우 전역 기동 reconcile이 돌아온다).
- 1을 건너뛰고 2를 하면 실행 중인 Pod의 채팅 요청이 실패한다. 이 순서의 실행 명령은 이 문서에 적지 않는다.

기동 회수는 best-effort다 — DB가 느리거나 잠겨 있어 실패하면 기동은 계속하고(readyz가 상태를
알린다), 회수는 그 사용자의 다음 요청이나 다음 기동이 이어받는다.

## 4. 검증한 것과 안 한 것

실행한 Postgres 통합 테스트(`tests/test_postgres_integration.py`, G-1 절):

- 살아 있는 A의 긴 SSE 중 B 기동 → A `completed`, 회수 0
- 기동 회수 범위: 만료 행·오래된 소유자 없는 행만, `heartbeat_at` 보존
- B·C 동시 기동: 유효 lease 회수 0 / 만료 행 한 번만 회수
- 정상 종료: 자기 행만 반납, heartbeat 스레드 정지
- heartbeat 없이 사라진 인스턴스(같은 프로세스 모사): 만료 전 회수 0, 만료 뒤 회수
- **실제 OS 프로세스 SIGKILL**(subprocess uvicorn): 만료 전 회수 0, 만료 뒤 회수
- 브라우저 연결 종료와 lease 회수 지표 분리
- 두 인스턴스 동시 접수에서 사용자당 활성 1개 유지
- 다른 인스턴스로 온 cancel이 소유자 스트림을 조기 종료, retry 소유자 = 받은 인스턴스
- 요청 시 회수 + 300초 규칙 유지(최근이면 409, 300초 지났으면 failed 후 접수)
- migration 0005 왕복(행 보존, 올린 뒤 기존 행은 소유자 NULL)
- **bridge**: 물리적으로 0004인 DB에서 기동·Ready, heartbeat 실패 0, 전역 reconcile 없음(최근 소유자
  없는 행 유지, 240초 지난 행만 회수), lease 없이 접수 → 앱을 켠 채 0005 적용 → 다음 접수가
  소유자·lease 기록, Ready 유지
- **idempotency 순서**: 같은 키 replay·충돌이 만료 행과 회수 메트릭을 바꾸지 않음(completion·retry),
  새 키 요청만 회수, 같은 키 동시 두 요청은 생성 1건 + replay 1건
- readyz가 alembic_version 잠금 중에도 제한 시간 안에 503(기동 회수가 잠금을 기다리지 않음)

"두 인스턴스"는 대부분 **같은 테스트 프로세스 안의 별도 앱**(각자 instance_id·heartbeat·uvicorn)이다.
실제 프로세스 종료는 SIGKILL 테스트 하나만 다뤘다.

미구현·미검증:

- 실제 두 Pod, Argo 롤아웃, 실제 migration Job 적용, bridge·기능 릴리스 이미지
- persona-platform replica 2·PDB·분산 변경(ROLL-01B 전제, 구현하지 않음)
- 브라우저 재연결(Web)과 생성 이어받기 — 계약상 없음
- 연결 종료·회수 시 GPU(vLLM) 생성 취소 — 연결 종료는 취소가 아니라는 결정을 유지
