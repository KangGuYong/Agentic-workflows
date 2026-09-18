# 런타임 코어(Plan 2a) 설계

MVP 설계: `docs/superpowers/specs/2026-09-11-agentic-workflow-builder-mvp-design.md` (이하 **MVP 문서**)
선행 구현: Plan 1 엔진 코어 (`services/engine`, PR #1)

이 문서는 MVP 문서의 하위 프로젝트 2(런타임 서비스)를 둘로 나눈 것 중 **앞쪽(2a)** 을 다룬다. 뒤쪽(2b)은 `http_request` 노드와 egress 정책, 시크릿 저장소, 헤더·값 기반 레닥션, 보존 기간 정리, 배포용 compose를 맡는다.

MVP 문서가 이미 정한 내용(상태 머신, 이벤트 스키마, API 계약, 제한)은 반복하지 않고 **구현 수준의 결정과 MVP 문서와 달라지는 점만** 적는다.

---

## 1. 목표와 완료 기준

에디터 없이 API만으로 다음이 되면 2a는 끝난 것이다.

1. 워크플로를 저장하고(낙관적 잠금), 검증하고, 실행을 생성한다.
2. 워커가 실행을 집어 끝까지 돌리고, 진행 상황이 SSE로 끊김·중복 없이 전달된다.
3. `human_approval`에서 멈춘 실행이 워커를 재시작한 뒤에도 승인으로 이어서 끝난다.
4. 노드 실행 중 워커를 죽여도 다른 워커가 이어받고, 체크포인트에 기록된 노드는 다시 실행하지 않는다.
5. 같은 실행을 두 워커가 동시에 진행하지 않는다.
6. 실행을 취소할 수 있고, 취소 시점의 상태에 따라 즉시 또는 진행 중 중단으로 처리된다.

MVP 문서 2.3의 성공 기준 2·3·4·6이 여기에 대응한다. 1·5는 Plan 3(에디터)에서 확인한다.

**2a 범위**: 데이터 계층, 실행 상태 채널 구조 변경, 이벤트 기록·발행, 워커(점유·리스·취소·복구·reaper), API 전체(시크릿 제외), 공유 토큰 인증, CPU 격리, 모델 세마포어, `storeRunData`와 키 이름 기반 레닥션, 개발용 compose.

**2b 범위**: `http_request` 노드, egress 정책, 시크릿 저장소와 `{{secret.NAME}}`, 헤더·값 기반 레닥션, 보존 기간 정리, 배포용 compose.

**코드 배치** (MVP 문서 3.1을 따른다)

```
services/engine/engine/
  db/          커넥션 풀, 질의, Alembic 마이그레이션
  events/      PostgresRecorder, 레닥션, Redis 발행
  worker/      엔트리포인트, 점유·리스·하트비트, reaper, 렌더 풀
  api/         FastAPI 앱, 라우터, 공통 계층
deploy/docker-compose.dev.yml   postgres, redis (개발용)
```

키 이름 레닥션만 2a에 두는 이유: `node_runs` 저장이 2a에서 시작되므로 그때부터 필요하다. 헤더 이름·시크릿 값 기반 레닥션은 `http_request`와 시크릿이 생겨야 의미가 있다.

---

## 2. MVP 문서에서 달라지는 점

| MVP 문서 | 변경 | 이유 |
|---|---|---|
| 3장: `api → Redis 큐(arq) → worker` | arq 제거. 실행 큐는 `runs` 테이블 + `LISTEN/NOTIFY` | 리스·CAS·reaper를 어차피 직접 구현하므로 큐는 깨우는 신호일 뿐이다. 큐와 DB가 어긋나는 경우가 사라지고 "커밋 후 큐 등록" 이중 쓰기가 없어진다 |
| 3.1: `engine/db/`에 SQLAlchemy 모델 | psycopg3 + 직접 작성한 SQL + Alembic(수동 마이그레이션). ORM 없음 | 중요한 질의가 전부 `UPDATE … RETURNING` CAS라 ORM 매핑이 층만 늘린다. LangGraph 체크포인터도 psycopg3라 드라이버가 하나로 통일된다 |
| 5.2: "60초 넘게 `queued`인 실행 → 큐 재등록" | 삭제. 점유 루프가 5초 주기로 폴링한다 | `NOTIFY`가 트랜잭션 안에서 나가므로 유실 경로는 "그 순간 듣는 워커가 없음"뿐이고, 폴링이 그것을 덮는다 |
| 5.2: reaper = arq cron | 워커 안의 주기 태스크. `pg_try_advisory_lock`으로 한 번에 한 워커만 수행 | 프로세스 종류를 늘리지 않는다 |
| 5.3: `RunState.outputs`가 노드 출력 전부를 담는 단일 채널 | 노드마다 `out_<id>` 채널로 분리(3절) | 단일 채널은 스텝마다 전체를 다시 저장해 체크포인트가 실행 길이의 제곱으로 늘어난다(측정: 노드 40개·입력 250KB → 226MB) |
| 11.1: 워커당 동시 실행 10 (arq `max_jobs`) | 동일하되 `asyncio.Semaphore`로 제한 | arq 제거에 따른 대체 |
| 3.3: `node_runs` 열 | `waited boolean` 추가 | Plan 1의 `find_waiting`이 "한 번이라도 대기했던 시도"를 찾는다. 이미 닫힌 대기 시도는 `status`로 구분되지 않는다 |

그 밖(상태 전이 표, 이벤트 타입, API 엔드포인트·오류 코드, 제한값, 보존 정책)은 MVP 문서 그대로다.

---

## 3. 실행 상태 채널 구조 (엔진 변경)

### 3.1 현재와 변경 후

지금은 모든 노드 출력이 채널 하나에 들어간다.

```python
class RunState(TypedDict, total=False):
    inputs: dict
    outputs: Annotated[dict[str, Any], merge_dicts]   # node_id -> 출력
    routes: Annotated[dict[str, list[str]], merge_dicts]
    loop_counters: Annotated[dict[str, int], merge_dicts]
    exec_counts: Annotated[dict[str, int], merge_dicts]
```

변경 후에는 `compile_workflow`가 워크플로의 노드 id로 상태 타입을 만든다.

- 전역 채널 넷(`inputs`, `routes`, `loop_counters`, `exec_counts`)은 그대로 둔다. 작고 모든 노드가 함께 쓴다.
- 노드마다 채널 `out_<node_id>`를 추가한다. 리듀서 없음(마지막 값). 각 노드는 자기 채널에만 쓰므로 병렬 쓰기 충돌이 구조적으로 생기지 않는다.
- 접두사 `out_`은 단사 사상이라 노드 id끼리 충돌하지 않고, 전역 채널 이름과도 겹치지 않는다. 노드 id는 이미 `^[a-z][a-z0-9_]{0,39}$`로 검증되므로 파이썬 식별자로도 유효하다.

### 3.2 영향 범위

| 위치 | 변경 |
|---|---|
| `engine/compiler/state.py` | `build_state_type(node_ids)`, `initial_state(inputs)`(전역 채널만), `outputs_of(state) -> dict[str, Any]` |
| `engine/compiler/wrapper.py` | 상태 쓰기를 `{"out_<id>": 출력, …}`로. 렌더링·라우팅 입력은 `outputs_of(state)`로 조립 |
| `engine/compiler/build.py` | `StateGraph(build_state_type(...))` |
| `engine/runtime/runner.py` | 최종 출력은 `outputs_of(state).get("end", {})` |

`CompiledWorkflow`의 캐시 키(`dsl_hash`)는 그대로 유효하다. 상태 타입이 DSL에서 파생되기 때문이다.

### 3.3 저장량

| | 지금 | 변경 후 |
|---|---|---|
| 한 스텝의 체크포인트 쓰기 | 전체 `outputs` 딕셔너리 | 그 노드의 출력 + 작은 전역 채널들 |
| 실행 전체 | O(스텝 수 × 전체 출력 크기) | O(노드 출력 크기 합계) — 루프는 반복 횟수만큼 더해지고 `maxIterations`로 상한이 있다 |

전역 채널은 매 스텝 다시 저장되지만 노드 id·정수·핸들 이름만 담아 실행당 수십 KB를 넘지 않는다.

---

## 4. CPU 격리 (엔진 변경)

| 작업 | 상한 | 처리 |
|---|---|---|
| 스키마 검증(`schema_violations`) | 단계 예산으로 약 3초 | `asyncio.to_thread` — GIL이 주기적으로 풀려 이벤트 루프가 계속 돈다 |
| 템플릿 렌더링 | 없음. 데이터를 훑는 루프 하나로 O(데이터²)가 된다(측정: 20k 항목 × 2k 반복 ≈ 7.7초) | 별도 프로세스 + 데드라인. 스레드로는 중단할 수 없다 |

엔진에는 훅 하나만 추가한다.

```python
# engine/runtime/deps.py
render: RenderFn | None = None   # None이면 지금처럼 인라인 실행
# RenderFn = Callable[[list[TemplateField], dict[str, Any]], Awaitable[dict[str, Any]]]
```

노드 래퍼는 `deps.render`가 있으면 그쪽으로 넘긴다. 인자는 템플릿 문자열과 출력 딕셔너리뿐이라 그대로 직렬화된다. Plan 1의 기존 테스트는 훅 없이 통과한다.

워커 구현은 `pebble.ProcessPool`을 쓴다. 작업 하나에 타임아웃을 걸고 시간이 넘으면 **그 워커 프로세스만** 죽인다(`concurrent.futures.ProcessPoolExecutor`는 실행 중인 작업을 죽이지 못해, 풀 전체를 재생성해야 하고 무관한 실행까지 말려든다). 데드라인 초과는 `NodeError(TEMPLATE_ERROR, 재시도 불가)`로 바꾼다.

메모리 상한은 풀 초기화 함수에서 `resource.setrlimit(RLIMIT_AS)`로 건다. 리눅스 컨테이너에서만 적용되고 윈도우 개발 환경에서는 건너뛴다.

---

## 5. 데이터 계층

### 5.1 드라이버와 마이그레이션

- **psycopg3(async)** 하나로 통일한다. 애플리케이션 질의, `LISTEN/NOTIFY`, LangGraph `AsyncPostgresSaver`가 모두 psycopg3를 쓴다.
- 커넥션 풀은 `psycopg_pool.AsyncConnectionPool` 하나를 API·워커가 각자 만든다. `LISTEN` 전용 연결은 풀 밖에서 따로 잡는다(알림 수신 연결은 다른 질의와 공유하지 않는다).
- 스키마는 Alembic으로 관리하되 마이그레이션은 손으로 쓴 SQL(`op.execute`)이다. ORM 모델이 없으므로 autogenerate는 쓰지 않는다.
- LangGraph 체크포인트 테이블은 `AsyncPostgresSaver.setup()`이 만든다. 마이그레이션 실행 후 기동 시 한 번 호출해 순서를 고정한다.

### 5.2 테이블

MVP 문서 3.3 그대로다. 2a에서 만드는 것은 `workflows`, `workflow_versions`, `runs`, `node_runs`, `run_events`이고 `secrets`는 2b에서 만든다.

추가로 정하는 인덱스와 제약:

| 테이블 | 인덱스·제약 |
|---|---|
| `workflows` | `index(workspace_id, updated_at desc)` |
| `workflow_versions` | `unique(workflow_id, dsl_hash)`, `unique(workflow_id, version_no)` |
| `runs` | `unique(workflow_id, idempotency_key)`(키가 있을 때만 적용되는 부분 인덱스), `index(status, created_at)` 점유용, `index(workflow_id, created_at desc)` 목록용, `index(lease_expires_at) where status='running'` reaper용 |
| `node_runs` | `unique(run_id, node_id, exec_index, attempt)`, `index(run_id, started_at)` |
| `run_events` | `primary key(run_id, seq)` |

`runs.status`에는 `queued|running|waiting|succeeded|failed|cancelled` 체크 제약을 건다. 상태 전이 규칙은 애플리케이션이 지키고, 제약은 오타 방지용이다.

### 5.3 체크포인트 암호화

`AsyncPostgresSaver`에 암호화 직렬화기를 **처음부터** 붙인다(키: 환경 변수 `LANGGRAPH_AES_KEY`). 나중에 붙이면 기존 체크포인트를 읽을 수 없다. 키가 없으면 기동을 거부하고, 개발 환경에서만 `ENGINE_DEV_INSECURE=1`로 우회할 수 있다(기동 시 경고 로그).

---

## 6. 워커

프로세스 하나가 asyncio로 다음을 함께 돌린다: 알림 수신 연결 1개, 점유 루프, 실행 태스크(최대 `WORKER_MAX_RUNS`, 기본 10), 실행마다 하트비트 태스크, reaper 주기 태스크, 렌더 프로세스 풀.

### 6.1 점유

```sql
UPDATE runs SET status='running', lease_owner=%(me)s,
       lease_expires_at=now() + interval '30 seconds',
       started_at=coalesce(started_at, now())
WHERE id = (SELECT id FROM runs WHERE status='queued'
            ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
RETURNING *
```

- 이 한 문장이 MVP 문서 5.2의 CAS 점유이자 큐다. 여러 워커가 동시에 돌려도 서로 다른 행을 가져간다.
- 루프: 동시 실행 여유가 있는 동안 위 질의를 반복 → 가져올 게 없으면 `runs_queued` 알림을 기다리되 **5초 타임아웃**. 타임아웃마다 다시 질의한다. 워커가 죽어 있는 동안 온 알림은 이 폴링이 덮는다.
- 점유 직후 `run_started` 이벤트를 기록하며, 그때까지 복구된 횟수(`recoveryCount`)를 싣는다. `run_recovered`는 워커가 아니라 reaper가 기록한다: 만료된 리스를 복구해 다시 큐에 넣는 행위 자체에 대한 이벤트다(6.5).

### 6.2 리스와 펜싱

10초마다 다음을 실행한다.

```sql
UPDATE runs SET lease_expires_at = now() + interval '30 seconds',
       active_ms = active_ms + %(delta_ms)s
WHERE id=%(id)s AND lease_owner=%(me)s AND status='running'
RETURNING cancel_requested_at, active_ms
```

- 0행이면 리스를 잃은 것이다 → 가드의 `lose_lease()` → 엔진이 `LeaseLost`로 빠져나온다. 워커는 **어떤 상태도 쓰지 않고** 조용히 끝낸다(새 소유자가 이어간다).
- `cancel_requested_at`이 있으면 가드의 `cancel()`.
- `active_ms`가 `RUN_MAX_ACTIVE_MS`(기본 1시간)를 넘으면 실행 태스크를 취소하고 `RUN_TIMEOUT`으로 `failed` 처리한다.
- 워커가 `runs`를 갱신하는 모든 문장에 `AND lease_owner=%(me)s`를 붙인다(펜싱).

### 6.3 취소

- 워커마다 Redis 채널 `runs:control` 하나를 구독한다. 메시지는 `{"runId": …, "action": "cancel"}`이고, 워커는 자기가 들고 있는 실행이면 가드에 전달한다. 실행마다 구독하지 않는다.
- Redis 유실 대비 경로는 하트비트의 `cancel_requested_at` 확인(최대 10초 지연)이다.
- 취소가 확정되면 `running`으로 남은 `node_runs` 행을 닫고(`cancelled`), 실행을 `cancelled`로 전이한다.

### 6.4 실행 루프

1. 점유 → 하트비트·제어 구독 시작.
2. `execute_run(compiled, deps=…, inputs=…, resume=…)`을 호출한다. `resume_payload`가 있으면 `resume=`으로 넘기고, 없으면 `inputs=`만 넘긴다. 체크포인트가 있으면 Plan 1의 `execute_run`이 알아서 이어서 실행한다.
3. 결과에 따라 전이한다.

| `execute_run` 결과 | 전이 | 비고 |
|---|---|---|
| `RunOutcome("succeeded")` | `succeeded` + `outputs` 저장 | `storeRunData=false`면 `inputs`를 지운다 |
| `RunOutcome("waiting")` | `waiting` + `waiting_node_id/exec_index` 저장, 리스 해제 | 대기 시간은 `active_ms`에 넣지 않는다 |
| `RunOutcome("failed")` | `failed` + `error` 저장 | |
| `RunOutcome("cancelled")` | `cancelled` | |
| `ResumeRejected` | 상태 유지(`waiting`) + `resume_payload` 비움 | API가 먼저 검사하므로 정상 경로에서는 나오지 않는다 |
| `LeaseLost` | 아무것도 쓰지 않음 | |
| `EngineFault`·기타 예외 | 하트비트를 멈추고 `lease_expires_at=now()`로 만들어 reaper가 복구하게 한다 | |

모든 전이는 상태 갱신과 이벤트 기록을 한 트랜잭션에서 한다. 재개한 호출이 어떤 결과로 끝나든 `resume_payload`는 비운다(남아 있으면 다음 재시도가 승인 응답을 다시 보내는 셈이 된다).

### 6.5 reaper

15초 주기. `pg_try_advisory_lock(<고정 키>)`에 성공한 워커만 수행한다.

| 대상 | 조건 | 처리 |
|---|---|---|
| 리스 만료된 `running` | 취소 요청 없음, `recovery_count < 3` | `queued`, `recovery_count+1`, 알림, `run_recovered` 이벤트 |
| 리스 만료된 `running` | `recovery_count >= 3` | `failed`(`ENGINE_RECOVERY_EXHAUSTED`) |
| 리스 만료된 `running` | 취소 요청 있음 | `cancelled` |
| `waiting` | 마지막 갱신이 30일 초과 | `cancelled` |

어느 경우든 `running`으로 남은 `node_runs` 행을 함께 닫는다.

---

## 7. 이벤트 기록과 발행

### 7.1 PostgresRecorder

Plan 1의 `Recorder` 프로토콜을 구현한다. 실행마다 인스턴스를 하나 만든다.

| 메서드 | 구현 |
|---|---|
| `attempts_so_far` | `SELECT count(*) FROM node_runs WHERE run_id, node_id, exec_index` |
| `find_waiting` | 같은 실행·노드·exec_index에서 `node_waiting`을 기록한 적 있는 시도 중 가장 큰 attempt(없으면 `None`) |
| `node_started` | `INSERT node_runs(running)` — 유니크 충돌은 `DuplicateAttempt`(다른 워커가 같은 실행을 돌리고 있다는 뜻) |
| `node_succeeded` / `node_failed` / `node_waiting` | 해당 행을 닫고 이벤트 기록. **이미 닫힌 행을 다시 닫아도 오류가 아니다**(재개 시 `node_finished`가 두 번 나올 수 있다) |
| `node_token` | Redis 발행만. 저장하지 않고 실패는 무시 |

`find_waiting`은 "지금 대기 중"이 아니라 "한 번이라도 대기했던" 시도를 돌려준다(Plan 1 규약). `node_runs`의 상태만으로는 이미 닫힌 대기 시도를 구분할 수 없으므로, 대기 여부를 나타내는 열 `waited boolean`을 `node_runs`에 추가한다.

순번 부여는 MVP 문서 7.2 그대로 한 트랜잭션에서 한다.

```sql
UPDATE runs SET event_seq = event_seq + 1 WHERE id=%(id)s RETURNING event_seq
-- 이어서 INSERT INTO run_events(...)
```

실행 행 잠금이 직렬화를 보장하므로 API와 워커가 같이 써도 번호가 겹치지 않는다. **커밋 후** Redis `run:{id}` 채널에 발행하고, 발행 실패는 무시한다(이벤트는 이미 Postgres에 있다).

### 7.2 저장 정책

- `storeRunData=false`면 `node_runs.input/output`, `run_events.payload`의 본문, 종료 시 `runs.inputs`를 저장하지 않는다(메타데이터는 남는다).
- 저장 직전 **키 이름 기반 레닥션**을 적용한다: 키가 `/(authorization|password|passwd|secret|token|api[_-]?key|cookie)/i`에 맞으면 값을 `[REDACTED]`로 바꾼다. 순회 깊이는 `engine.jsondata.MAX_JSON_DEPTH`를 따른다.
- `node_runs.input/output`은 각각 256KB를 넘으면 잘라 저장하고 `truncated=true`. 이벤트 미리보기는 4KB.
- 저장하는 모든 텍스트는 `check_text`를 통과해야 한다(jsonb가 NUL을 거부한다). Plan 1이 노드 출력·오류 메시지에서 이미 보장하지만, 기록 계층에서도 마지막으로 막는다.

### 7.3 SSE

`GET /runs/{id}/events`, MVP 문서 7.3의 순서를 그대로 구현한다.

1. Redis `run:{id}` 구독을 **먼저** 열고 수신분을 버퍼에 쌓는다.
2. Postgres에서 `seq > after`를 읽어 보낸다.
3. 버퍼와 이후 실시간 메시지를 보내되 이미 보낸 `seq` 이하는 버린다.
4. 받은 `seq`가 마지막+1보다 크면 Postgres에서 빈 구간을 읽어 먼저 보낸다.

- 영속 이벤트에만 `id: <seq>`를 붙인다. `node_token`에는 붙이지 않는다(브라우저의 `Last-Event-ID`가 토큰 때문에 전진하면 안 된다).
- 15초마다 `: ping`. 종료 이벤트를 보낸 뒤 닫고, `waiting` 중에는 열어 둔다.
- 브라우저 `EventSource`는 헤더를 못 붙이므로, 토큰 인증을 쓸 때 에디터는 `fetch` 기반 SSE 클라이언트를 써야 한다(Plan 3 메모). 쿼리 문자열로 토큰을 받지 않는다.

---

## 8. API

### 8.1 공통 계층

| 계층 | 동작 |
|---|---|
| 인증 | `Authorization: Bearer <토큰>`을 `ENGINE_API_TOKEN`과 `hmac.compare_digest`로 비교. 환경 변수가 없으면 인증 없이 통과하고 기동 시 경고를 남긴다. 실패는 `401 UNAUTHORIZED` |
| 크기 | 본문이 `MAX_BODY_BYTES`(1MB)를 넘으면 `413 PAYLOAD_TOO_LARGE`. DSL 512KB·실행 입력 256KB는 각 엔드포인트에서 확인 |
| JSON | 본문을 `engine.jsondata.parse_json`으로 읽는다. FastAPI 기본 파서는 `NaN`과 중복 키를 받아들인다 |
| 오류 | MVP 문서 8.2 형식(`{"error": {"code", "message", "details"}}`). 처리하지 못한 예외는 500 + `INTERNAL`, 본문에 내부 정보를 넣지 않는다 |
| CPU | `validate`와 실행 생성의 검증은 스레드로 넘긴다 |

### 8.2 엔드포인트

MVP 문서 8.1에서 `/secrets`를 뺀 전부. 추가로 `GET /healthz`(DB·Redis 확인).

정하는 세부 사항:

- **`POST /workflows/{id}/runs`** — MVP 문서 8.3의 트랜잭션을 그대로 따르고, 마지막에 같은 트랜잭션에서 `NOTIFY runs_queued`를 보낸다. 커밋되지 않으면 알림도 나가지 않으므로 이중 쓰기가 없다.
- **`POST /runs/{id}/resume`** — `waiting → queued` 전에 `engine.nodes.human_approval.resume_output(body, waiting)`으로 검사한다(실패 시 `422 VALIDATION_FAILED`). `nodeId/execIndex`가 대기 중인 것과 다르면 `409 RESUME_TARGET_MISMATCH`. `reviewedAt`은 요청 값을 버리고 서버 시각으로 채운다. 검사를 통과한 본문을 `resume_payload`에 저장하고 알림을 보낸다.
- **`POST /runs/{id}/retry`** — `failed`만 가능. 체크포인트가 없으면 `409 RUN_DATA_EXPIRED`. `retry_count+1`, `queued`, 알림.
- **`POST /runs/{id}/cancel`** — `queued`·`waiting`은 즉시 `cancelled`. `running`은 `cancel_requested_at`을 기록하고 Redis `runs:control`에 발행한 뒤 현재 상태를 돌려준다. 이미 종료된 실행은 `409 INVALID_STATE_TRANSITION`.
- **`GET /runs/{id}`** — `waitingFor`에 대기 중인 승인 페이로드(`nodeId, execIndex, message, review, allowEdit`)를 담는다.
- **`GET /node-types`** — Plan 1의 레지스트리에서 만든다. 2a에서는 8종, 2b에서 `http_request`가 더해진다.

### 8.3 레지스트리 하나 쓰기

`analyze`·`validate`·`compile_workflow`는 `registry=`를 생략하면 각자 기본 레지스트리를 새로 만든다. 2b에서 `http_request`가 등록되면 이것이 같은 DSL을 다르게 판정하게 된다. API·워커는 **프로세스마다 레지스트리 하나**를 만들어 모든 호출에 명시적으로 넘긴다.

### 8.4 컴파일 캐시

워커는 `dsl_hash → CompiledWorkflow` 캐시를 프로세스 안에 둔다(`functools.lru_cache` 수준, 최대 32개). 캐시 키가 `dsl_hash`만으로 유효한 것은 프로세스마다 체크포인터와 레지스트리가 하나뿐일 때이므로(Plan 1 메모), 이 둘을 모듈 수준에서 한 번만 만든다.

---

## 9. Redis 사용

Redis는 전송·조정 수단이고, 모두 잃어도 Postgres에서 복구된다.

| 용도 | 키·채널 | 비고 |
|---|---|---|
| 이벤트 발행 | 채널 `run:{id}` | SSE가 구독. 유실 시 SSE가 Postgres에서 빈틈을 채운다 |
| 실행 제어 | 채널 `runs:control` | 취소 신호. 유실 시 하트비트가 DB에서 확인한다 |
| 모델 세마포어 | 정렬 집합 `llm:sem:{model}` | 아래 |

세마포어는 TTL이 있는 정렬 집합으로 만든다: 획득 시 `ZREMRANGEBYSCORE`로 만료분을 지우고 `ZCARD < limit`이면 `ZADD(now, token)`, 반납은 `ZREM`. 호출이 길면 30초마다 점수를 갱신하고, 워커가 죽으면 TTL(120초)이 지나 자동으로 회수된다. 한도 기본값은 `OLLAMA_NUM_PARALLEL`.

---

## 10. 설정

| 환경 변수 | 기본값 | 용도 |
|---|---|---|
| `ENGINE_DATABASE_URL` | — | Postgres |
| `ENGINE_REDIS_URL` | — | Redis |
| `ENGINE_API_TOKEN` | 없음 | 공유 토큰(없으면 인증 없음) |
| `LANGGRAPH_AES_KEY` | — | 체크포인트 암호화 |
| `ENGINE_DEV_INSECURE` | `0` | 개발용: 암호화 키 없이 기동 허용 |
| `OLLAMA_BASE_URL` / `OLLAMA_NUM_PARALLEL` | — / `1` | LLM |
| `WORKER_MAX_RUNS` | `10` | 워커당 동시 실행 |
| `WORKER_LEASE_SEC` / `WORKER_HEARTBEAT_SEC` | `30` / `10` | 리스 |
| `RUN_MAX_ACTIVE_MS` | `3600000` | 실행 활성 시간 상한 |
| `RENDER_TIMEOUT_SEC` / `RENDER_POOL_SIZE` | `5` / CPU 수 | 렌더 프로세스 풀 |

---

## 11. 테스트

testcontainers로 세션당 Postgres·Redis를 띄우고 Alembic을 올린다. 테스트마다 애플리케이션 테이블과 체크포인트 테이블을 비운다. 워커는 별도 프로세스가 아니라 테스트 안에서 asyncio 태스크로 띄운다. LLM은 Plan 1의 `ScriptedLLM`을 쓴다.

| 수준 | 대상 |
|---|---|
| 단위 | 레닥션(키 이름, 크기 자르기, `storeRunData`), 세마포어(한도·TTL 회수), 노드별 채널(저장량이 스텝 수에 비례하지 않음), 렌더 데드라인(프로세스가 죽고 다음 호출이 정상 동작) |
| 통합·상태 머신 | 허용·금지 전이 전부, 두 워커가 동시에 점유 시도 → 하나만 성공 |
| 통합·복구 | 노드 실행 중 워커 태스크 강제 종료 → reaper 복구 → 완료 노드 비재실행, `recovery_count` 한도 초과 시 `failed` |
| 통합·HITL | 대기 → 워커 재시작 → resume → 완료, 잘못된 대상 409, 잘못된 본문 422, 재전송된 응답이 다음 승인을 답하지 않음 |
| 통합·취소 | `queued`·`waiting`·`running` 각각, 취소 후 `node_runs`에 `running`이 남지 않음 |
| 통합·이벤트 | `seq`에 빈틈 없음, SSE 재연결(`Last-Event-ID`) 중복·유실 없음, Redis 발행을 일부러 빠뜨려도 빈틈이 채워짐 |
| 통합·API | 낙관적 잠금 409, 멱등 키 재사용 시 같은 `runId`, 같은 해시의 버전 재사용, 토큰 없는 요청 401 |
| 통합·전체 | 골든 워크플로 5종(Plan 1의 픽스처)을 API로 실행해 끝까지 완료 |

---

## 12. 열린 위험

| 위험 | 대응 |
|---|---|
| 노드별 채널 전환이 Plan 1의 테스트 90여 개를 건드린다 | 독립 태스크로 두고 스펙 준수·품질 리뷰를 각각 받는다. 저장량 감소를 측정하는 테스트를 함께 추가한다 |
| `pebble` 의존성 추가 | 대안은 풀 전체 재생성(무관한 실행까지 영향). 먼저 작은 스파이크로 윈도우·리눅스 동작을 확인하고, 문제가 있으면 직접 만든 프로세스 감독으로 대체한다 |
| `EncryptedSerializer` API가 LangGraph 버전에 묶여 있다 | 버전을 고정하고, 직렬화기를 감싼 얇은 어댑터를 둔다 |
| psycopg3 `LISTEN` 연결이 끊기면 알림을 놓친다 | 알림은 최적화일 뿐이다. 5초 폴링이 정확성을 책임진다. 연결이 끊기면 재연결한다 |
| 실행 하나가 `runs` 행 잠금을 길게 잡으면 순번 부여가 밀린다 | 순번 트랜잭션은 `UPDATE … RETURNING` + `INSERT` 두 문장뿐이고 그 안에서 다른 I/O를 하지 않는다 |