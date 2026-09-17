# HTTP·보안·운영(Plan 2b) 설계

MVP 설계: `docs/superpowers/specs/2026-09-11-agentic-workflow-builder-mvp-design.md` (이하 **MVP 문서**)
선행: Plan 1 엔진 코어(PR #1), Plan 2a 런타임 코어(PR #2)

이 문서는 MVP 문서의 하위 프로젝트 2(런타임 서비스)를 둘로 나눈 것 중 **뒤쪽(2b)** 을 다룬다. Plan 2a가 남긴 것 — `http_request` 노드와 egress 정책, 시크릿 저장소와 `{{secret.NAME}}`, 헤더·값 기반 레닥션, 보존 기간 정리, 배포용 compose — 에 2a 브랜치 리뷰가 Plan 2b로 미룬 하드닝 4건을 더한다.

MVP 문서가 이미 정한 내용은 반복하지 않고 **구현 수준의 결정과 MVP 문서와 달라지는 점만** 적는다.

---

## 1. 목표와 완료 기준

에디터 없이 API만으로 다음이 되면 2b는 끝난 것이다.

1. 시크릿을 저장하고, `http_request`에서 `{{secret.NAME}}`으로 참조해 실제 요청을 보내고, 그 값이 `node_runs`·`run_events`·체크포인트·API 응답·서비스 로그 어디에도 나타나지 않는다.
2. allowlist에 없는 호스트, 사설·특수 IP로 해석되는 호스트, 그리고 **리다이렉트로 그런 곳에 닿는 요청**이 `HTTP_BLOCKED`으로 차단되고 차단 사유에 범주 이름만 남는다.
3. 보존 기간이 지난 종료 실행은 메타데이터만 남기고 페이로드와 체크포인트를 잃는다. 삭제된 워크플로가 고아 체크포인트를 남기지 않는다.
4. fan-out 한도까지 채운 워커가 풀 고갈로 **정상 실행의 리스를 잃지 않는다**.
5. `docker compose -f deploy/docker-compose.yml up`으로 postgres·redis·api·worker가 뜨고, 외부 Ollama를 가리켜 워크플로가 끝까지 돈다.

MVP 문서 2.3의 성공 기준 중 데이터·보안 항목이 여기에 대응한다.

**2b 범위**: `http_request` 노드, egress 정책, 시크릿 저장소와 API, 헤더·값 기반 레닥션, 보존 기간 정리와 고아 체크포인트 수거, 2a 리뷰가 미룬 하드닝 4건, 보안 태세 변경(토큰 기동 거부, `runs.inputs/outputs` 암호화), 배포용 compose와 Dockerfile.

**2b 비범위**: 에디터(Plan 3), 계정·권한, 자동 PII 탐지, 실행 기록 내보내기, 다중 워크스페이스.

**코드 배치**

```
services/engine/engine/
  http/        egress 정책, 가드된 HTTP 클라이언트
  nodes/       http_request.py 추가
  secrets/     저장소, 암복호화, 마커 치환
  db/          migrations/versions/0002_*.py, secrets 질의, purge 질의
  api/routers/ secrets.py 추가
deploy/
  docker-compose.yml   배포용 (postgres, redis, api, worker)
  .env.example
services/engine/Dockerfile
```

---

## 2. MVP 문서에서 달라지는 점

| MVP 문서 | 변경 | 이유 |
|---|---|---|
| 10.3: "해당 실행에서 렌더링된 모든 시크릿 값"을 저장 대상 문자열에서 치환 | 값 기반 레닥션을 `http_request.execute()` **반환 직전 경계**에서만 수행 | 실행 단위 레지스트리는 워커가 실행 내내 평문 시크릿을 들고 있게 만든다. 평문이 존재할 수 있는 유일한 지점에서 끝내면 레지스트리가 필요 없고 수명이 짧아진다. 시크릿은 노드 출력에서 이미 지워지므로 하류로 흐를 수도 없다 |
| 10.1: `runs.inputs`는 "레닥션 후" 저장 | 레닥션하지 않고 **AES 암호문으로 저장**, 읽기 경로에서 복호화 후 레닥션 | `runs.inputs`는 워커가 초기 상태를 만들 때 쓰는 값이라 저장 시점에 레닥션하면 실행이 깨진다. 두 요구는 동시에 만족할 수 없으므로 저장은 암호화로, 전송은 레닥션으로 나눈다. `runs.outputs`도 같이 암호화한다 |
| 8.1: `ENGINE_API_TOKEN`이 없으면 경고 후 인증 없이 통과 | **기동 거부**. `ENGINE_DEV_INSECURE=1`일 때만 통과 | `http_request`가 생기면 열린 포트는 데이터 열람을 넘어 "이 서버의 네트워크에서 요청을 보내는 권한"이 된다. `LANGGRAPH_AES_KEY`와 같은 규칙으로 맞춘다 |
| 10.4: "Reaper가 매일 수행" | 매 sweep마다 작은 배치로 수행. 마지막 실행 시각을 기록하지 않는다 | 질의가 인덱스를 타고 따라잡은 뒤에는 빈 결과라 비용이 없다. 하루 주기를 관리하면 워커가 자주 재시작하는 환경에서 날짜를 건너뛴다 |
| 10.5: allowlist 항목 문법 | 기동 시 파싱하고 잘못된 항목은 `ConfigError`로 기동 거부. `*.example.com`은 **하위 라벨만** 매칭(자기 자신 제외). 스킴과 기본 포트가 어긋난 항목(`http://host:443`)은 파싱 오류 | 런타임에 조용히 무시하면 운영자가 열었다고 믿는 항목이 닫혀 있거나 그 반대가 된다 |
| 4.7: `http_request`의 비-2xx 응답 처리 미명시 | 4xx/5xx는 **노드 오류**. 5xx·429는 재시도 가능, 4xx는 불가 | 비개발자에게는 오류가 오류로 보이는 쪽이 안전하다. "실패해도 계속"은 기존 `policy.onError=default`로 덮는다 |
| (신규) | 시크릿 값 **최소 8자**를 API에서 강제 | 짧은 값을 허용하면 값 기반 레닥션이 응답 전체를 `[REDACTED]`로 갈아버린다 |
| (신규) | 보존 정리가 **고아 체크포인트**(대응하는 `runs` 행이 없는 `thread_id`)도 수거 | 워크플로 삭제가 `runs`를 cascade로 지우면 체크포인트만 남는다(2a Task 12 리뷰가 남긴 구멍) |

그 밖(상태 머신, 이벤트 타입, 오류 형식, 제한값)은 MVP 문서와 Plan 2a 설계 그대로다.

---

## 3. `http_request` 노드와 두 개의 새 포트

### 3.1 노드

MVP 문서 4.7의 9번째 노드다. `NodeSpec` 규약(4.6)을 그대로 따른다.

| 항목 | 값 |
|---|---|
| `type` / `label` / `category` | `http_request` / "HTTP 요청" / `action` |
| Config | `method`(GET·POST·PUT·PATCH·DELETE·HEAD), `url`(템플릿), `headers`(이름→템플릿 맵), `body`(템플릿, 선택), `sendIdempotencyKey`(bool, 기본 false) |
| 템플릿 필드 | `url`, `headers`의 각 값, `body` — **이 세 곳에서만** `{{secret.NAME}}`이 허용된다 |
| Output | `{status: integer, headers: object, body: unknown}` |
| 핸들 | `out` |
| 기본 정책 | 30초, 멱등 메서드 3회 / POST·PATCH 1회, `onError=fail` |
| `side_effects` | `true` |

`sendIdempotencyKey=true`이면 `Idempotency-Key` 헤더를 붙인다. 값은 `sha256(run_id, node_id, exec_index)`의 앞 32자 — **attempt는 넣지 않는다**. 같은 실행 지점의 재시도가 같은 키를 보내야 상대 서버가 중복을 걸러낼 수 있기 때문이다(MVP 문서 5.4).

응답 바디 해석:

- `application/json` 또는 `+json` → 엔진의 엄격 파서 `parse_json`으로 파싱한 값
- `text/*` → 문자열
- 그 외 → 재시도 불가 `HTTP_UNSUPPORTED_MEDIA_TYPE`. DSL 타입 체계에 바이너리가 없으므로 억지로 담지 않는다

노드 출력은 기존 `_check_output`의 1MB 제한을 그대로 받는다(5MB 응답 제한과는 별개다 — 5MB는 "읽지도 않는다"는 뜻이다).

### 3.2 새 포트 두 개

`NodeContext`에는 지금 `llm`, `on_token`, `interrupt`가 있고 네트워크와 시크릿에 해당하는 것이 없다. `llm`이 주입되는 경로(`RunDeps` → `NodeContext`)를 그대로 따라 두 개를 더한다.

```python
class HttpClient(Protocol):
    async def request(self, *, method: str, url: str, headers: dict[str, str],
                      body: str | None, timeout_sec: float) -> HttpResponse: ...

class SecretResolver(Protocol):
    async def resolve(self, names: set[str]) -> dict[str, str]: ...
```

- `HttpClient`의 유일한 구현은 §5의 가드된 클라이언트다. 노드는 httpx를 모른다 — egress 정책을 노드 없이, 노드를 네트워크 없이 시험할 수 있다.
- `SecretResolver`는 `execute()` 안에서 요청 직전에만 호출된다. 실행 시작에 미리 모아두지 않는다.
- 둘 다 `None`이면 `http_request` 노드가 `EngineFault`를 올린다(설정 실수이지 테넌트 오류가 아니다).

---

## 4. 시크릿

### 4.1 저장

`secrets` 테이블(마이그레이션 `0002`):

| 열 | 타입 | 비고 |
|---|---|---|
| `workspace_id` | `uuid` | `name`과 함께 복합 기본키. MVP는 단일 워크스페이스지만 키를 지금 잡아두면 나중에 마이그레이션이 필요 없다 |
| `name` | `text` | `^[A-Z][A-Z0-9_]{0,63}$` |
| `ciphertext` | `bytea` | AES-GCM(nonce ‖ 암호문 ‖ 태그) |
| `created_at` / `updated_at` | `timestamptz` | |

키는 `ENGINE_SECRET_KEY`(16/24/32바이트). 체크포인트 키(`LANGGRAPH_AES_KEY`)와 **분리한다** — 하나가 새도 다른 하나는 버틴다. AAD로 `name`을 넣어 다른 이름의 암호문을 옮겨 붙이는 것을 막는다.

### 4.2 API — 쓰기 전용

| 메서드 | 경로 | 동작 |
|---|---|---|
| `PUT` | `/secrets/{name}` | 생성 또는 교체. 바디 `{"value": "..."}`. 값 **8자 이상 4KB 이하** |
| `DELETE` | `/secrets/{name}` | 삭제. 없으면 404 |
| `GET` | `/secrets` | 이름과 시각만. **값은 어떤 형태로도 반환하지 않는다** |

값을 읽는 엔드포인트는 "마스킹해서 준다"가 아니라 **존재하지 않는다**. 이름 목록은 에디터가 `{{secret.NAME}}` 자동완성에 쓴다.

### 4.3 마커 렌더링

렌더 풀의 Jinja 환경에 `secret` 바인딩을 넣되, 속성 접근이 값이 아니라 **마커**를 돌려준다.

```
[[secret:NAME:<nonce>]]
```

`nonce`는 `hmac(ENGINE_SECRET_KEY, run_id)`의 앞 16자리 hex로 **결정적으로 유도한다**. 저장하지 않으며, 워커가 재시작해 같은 실행을 재생해도 같은 마커가 나온다 — 재생 시 렌더 결과가 달라지면 기록과 체크포인트가 어긋난다. 테넌트 텍스트가 마커를 위조할 수 없는 것도 nonce 덕이다.

결과적으로:

- 시크릿 값이 **렌더 서브프로세스에 들어가지 않는다**. 그 프로세스는 죽여도, 덤프해도 얻을 것이 없다.
- `node_runs.input`에 기록되는 것은 마커가 든 문자열이다. 이것이 MVP 문서 10.1이 말하는 "렌더링 전 템플릿"에 해당하며, 에디터에서 "여기에 시크릿이 들어갔다"로 읽힌다.
- `execute()`가 요청 직전에 마커를 실제 값으로 치환한다. 치환 결과는 요청에만 쓰이고 어디에도 기록되지 않는다.

`RenderFn` 시그니처에 `secret_nonce: str | None`을 더한다. `None`이면 Jinja 환경에 `secret` 바인딩 자체가 없어 `{{secret.X}}`가 실패한다 — `http_request` 외의 노드에 대한 이중 방어다.

### 4.4 검증

2a가 이미 넣어둔 규칙을 살린다. `engine/validator/refs.py`는 `secret` 루트를 전부 `SECRET_NOT_ALLOWED`로 거부하는데, 여기에 예외를 연다: 노드 타입이 `http_request`이고 필드가 `url`·`headers`·`body`일 때만 허용. 그 밖의 위치는 그대로 오류다(LLM 프롬프트로의 유출 차단).

- 존재하지 않는 이름 참조 → 실행 생성 시 `VALIDATION_FAILED`
- 버전이 고정된 **뒤에** 시크릿이 삭제되면 → 실행 중 재시도 불가 `SECRET_NOT_FOUND`

---

## 5. Egress 정책

`engine/http/policy.py`(allowlist 파싱, 호스트 매칭, IP 분류)와 `engine/http/client.py`(가드된 클라이언트).

### 5.1 요청·홉마다 반복하는 절차

```
URL
 → 스킴 검사 (http, https만)
 → allowlist 검사        ← DNS보다 먼저
 → A/AAAA 전체 resolve
 → 모든 IP 분류·검사
 → 통과한 IP 하나로 고정
 → 요청
 → 3xx면 새 URL로 전부 다시
```

allowlist를 DNS보다 먼저 보는 것이 중요하다. 허용되지 않은 이름은 **해석조차 하지 않으므로** 호스트 이름 자체를 DNS exfiltration 채널로 쓸 수 없다.

### 5.2 allowlist 문법

`HTTP_ALLOWLIST`는 쉼표로 구분한 `scheme://host[:port][;allowPrivate]` 목록이다. 기동 시 파싱하고, 형식이 틀리면 `ConfigError`로 기동을 거부한다. 기본값은 빈 목록 = 전면 차단.

| 항목 | 의미 |
|---|---|
| `https://api.example.com` | 그 호스트, 포트 443만 |
| `https://api.example.com:8443` | 그 호스트, 포트 8443만 |
| `https://*.example.com` | `a.example.com`, `a.b.example.com` 매칭. **`example.com` 자체는 매칭하지 않는다** |
| `http://10.0.0.7:8080;allowPrivate` | 그 항목으로 매칭된 요청에만 사설 IP 허용 |
| `http://host:443`, `https://host:80` | **파싱 오류** — 스킴과 기본 포트가 어긋난다 |

`allowPrivate`는 전역 스위치가 아니다. 그 항목이 매칭됐을 때만 §5.3의 사설 검사를 면제하며, 다른 항목으로 매칭된 요청에는 영향이 없다.

IP 리터럴 URL은 allowlist에 그 리터럴이 직접 들어 있지 않으면 §5.1의 두 번째 단계에서 거부된다. 들어 있으면 §5.3을 그대로 적용한다. 핵심은 **IP 리터럴이 allowlist 우회 수단이 되지 않는다**는 것이다.

### 5.3 IP 분류

`ipaddress.is_private` 한 줄에 의존하지 않고 범주를 나열해 범주별로 시험한다. 해석된 **모든** 주소가 통과해야 하며, 하나라도 걸리면 요청 전체를 거부한다 — split answer로 검사를 통과한 뒤 금지된 주소로 연결되는 것을 막는다.

| 범주 | IPv4 | IPv6 |
|---|---|---|
| loopback | `127.0.0.0/8` | `::1` |
| link-local | `169.254.0.0/16` (클라우드 메타데이터) | `fe80::/10` |
| private / ULA | `10/8`, `172.16/12`, `192.168/16` | `fc00::/7` |
| CGNAT | `100.64.0.0/10` | — |
| unspecified | `0.0.0.0/8` | `::` |
| multicast | `224.0.0.0/4` | `ff00::/8` |
| reserved | `240.0.0.0/4` | 그 외 예약 범위 |

**IPv4-mapped/embedded IPv6는 풀어서 내부 주소를 다시 분류한다** — `::ffff:127.0.0.1`은 고전적인 우회 경로다.

### 5.4 연결 고정

해석한 주소를 다시 해석하지 않는다.

```
URL host    = api.example.com
connect IP  = 1.2.3.4            (검사를 통과한 주소)
Host header = api.example.com
TLS SNI     = api.example.com
인증서 검증 = api.example.com 기준
```

httpx/httpcore에서는 요청 URL의 호스트를 IP 리터럴로 바꾸고, `Host` 헤더를 원래 이름으로 두고, `extensions={"sni_hostname": <이름>}`을 넘기면 `httpcore`가 그것을 TLS의 `server_hostname`으로 쓴다(코드 확인 완료). 단순히 URL만 IP로 바꾸면 인증서 검증이 IP 기준이 되어 깨진다.

**이 성질은 Task 0 스파이크에서 실제 TLS 서버로 증명한 뒤에 나머지를 쌓는다**(§11.1).

### 5.5 리다이렉트와 한도

- `follow_redirects=False`, 최대 3홉을 직접 따라간다. 매 홉마다 §5.1 전체를 다시 수행한다. 3홉 제한이 리다이렉트 루프도 같이 막는다.
- `https → http` 다운그레이드 거부.
- 요청 바디 1MB, 응답 5MB(초과 시 읽기를 중단하고 `HTTP_RESPONSE_TOO_LARGE`).
- 타임아웃은 `policy.timeoutSec`(최대 120)을 연결과 전체 양쪽에 적용.
- TLS 인증서 검증은 항상 켜져 있고 끄는 옵션이 없다. 쿠키 저장소를 쓰지 않는다.

### 5.6 오류 코드

MVP 문서 8.2의 실행 오류 목록에 이미 `HTTP_BLOCKED`, `HTTP_ERROR`, `HTTP_RESPONSE_TOO_LARGE`가 있다. 그대로 쓰고 두 개만 더한다.

| 코드 | 상황 | 재시도 |
|---|---|---|
| `HTTP_BLOCKED` | egress 정책 위반. 메시지에 범주 이름만(호스트·IP 값 없음) | 불가 |
| `HTTP_ERROR` | 비-2xx 응답, 연결·읽기 실패, 타임아웃 | **5xx·429·전송 실패·타임아웃은 가능, 4xx는 불가** |
| `HTTP_RESPONSE_TOO_LARGE` | 5MB 초과 | 불가 |
| `HTTP_UNSUPPORTED_MEDIA_TYPE` (신규) | JSON도 텍스트도 아닌 응답 | 불가 |
| `SECRET_NOT_FOUND` (신규) | 버전 고정 뒤 시크릿이 삭제됨 | 불가 |

노드 자신의 마감(`policy.timeoutSec`)을 넘긴 경우는 기존 `NODE_TIMEOUT`이 그대로 맡는다.

---

## 6. 레닥션 — 두 계층

**1. 헤더 이름 기반.** `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`, `X-API-Key`, `X-Auth-Token`의 값은 요청·응답 헤더를 기록할 때 `[REDACTED]`. 시크릿을 쓰지 않은 요청에도 항상 적용된다.

**2. 시크릿 값 기반.** `http_request.execute()`가 반환하기 직전에, 이번 호출에서 사용한 값들을 **저장·전송될 모든 문자열**에서 치환한다:

- 응답 바디의 중첩된 문자열 전부
- 응답 헤더 값
- 기록되는 요청 URL·헤더·바디
- **예외 메시지**

규칙:

- 단순 부분 문자열 치환이다. `xxabc123yy`도 걸려야 한다(토큰 경계를 보지 않는다).
- 같은 값이 여러 번 나오면 전부 치환한다.
- 여러 시크릿이 서로 겹치면 **긴 것부터** 치환한다.
- 값 최소 길이 8자(§4.2)가 이 치환의 안전장치다.

예외도 노드 안에서 끝낸다. 렌더된 URL의 쿼리 문자열에 시크릿이 들어갈 수 있으므로 httpx 예외를 그대로 올리지 않고, 노드가 잡아 **호스트와 경로만** 담은 `NodeError`로 바꾼다. 서비스 로그도 같은 규칙이다(MVP 문서 10.1).

하류 노드가 `[REDACTED]`를 보는 것은 의도된 동작이다. 시크릿 값을 쓸 정당한 이유가 있는 하류 노드는 없으며, 원값을 상태에 남기면 체크포인트·기록·SSE가 전부 오염된다.

---

## 7. 보존 기간 정리

`RUN_DATA_RETENTION_DAYS`(기본 30). reaper가 이미 어드바이저리 락 아래에서 도는 주기 작업이므로 거기에 붙인다. **매 sweep마다 작은 배치**(기본 100행)를 처리하고 마지막 실행 시각을 기록하지 않는다 — 따라잡은 뒤에는 빈 결과라 비용이 없고, 워커가 자주 재시작해도 날짜를 건너뛰지 않으며, 긴 트랜잭션도 만들지 않는다.

기한이 지난 종료 실행 하나마다:

| 지우는 것 | 남기는 것 |
|---|---|
| `runs.inputs`, `runs.outputs` | `id`, `status`, `created_at`, `started_at`, `finished_at`, 토큰 수, 오류 코드, `retry_count`, `recovery_count`, `purged_at` |
| `node_runs.input`, `node_runs.output` | `node_id`, `exec_index`, `attempt`, `status`, 시각, 토큰 수, `truncated` |
| `run_events.payload` | `seq`, `type`, `node_id`, 시각 |
| `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`의 해당 `thread_id` | — |

`runs.purged_at`(마이그레이션 `0002`)이 이미 정리한 실행을 다시 훑지 않게 한다 — 조건은 `finished_at < now() - interval AND purged_at IS NULL`이고 인덱스도 거기에 맞춘다. 체크포인트가 사라진 `failed` 실행의 재시도는 2a가 이미 `RUN_DATA_EXPIRED`로 답한다.

**고아 체크포인트 수거.** 같은 sweep에서 대응하는 `runs` 행이 없는 `thread_id`를 지운다. 단, **행 자체가 1시간 이상 지난 것만** 대상으로 한다. 체크포인트는 `runs` 행이 생긴 뒤에만 쓰이므로 논리적으로 레이스가 없지만, 시간으로 한 겹 감싸두면 앞으로 생길 다른 경로에도 안전하다. `checkpoint_migrations`는 건드리지 않는다.

---

## 8. 2a 브랜치 리뷰가 미룬 하드닝 4건

**8.1 하트비트 전용 연결.** 워커가 `start()`에서 `LISTEN` 연결과 나란히 전용 연결 하나를 열고, 모든 하트비트가 `asyncio.Lock` 뒤에서 공유한다(짧은 UPDATE라 직렬화 비용이 없다). 풀 획득 타임아웃은 라이브러리 기본값 30초(= `WORKER_LEASE_SEC`)에서 **5초**로 내려, 고갈이 조용히 리스를 먹는 대신 빠르게 로그로 드러나게 한다. 그리고 **하트비트가 `lease_sec`를 넘겨 연속 실패하면 스스로 리스를 잃은 것으로 간주해 `guard.lose_lease()` + `task.cancel()`을 부른다.**

측정 근거: 10실행 × 10분기에서 풀 14개 중 13개 사용, 대기 7건. psycopg 기본 획득 타임아웃(30초)이 `WORKER_LEASE_SEC`(30초)와 같아, 풀을 기다리다 리스를 잃은 하트비트가 그 사실을 배우지 못한 채 실행을 계속하고 reaper가 같은 실행을 다른 워커에 넘긴다.

**8.2 SSE는 죽지 말고 낮아진다.** Redis가 없으면 펌프가 끝나고 지금은 스트림도 같이 끝난다(200 헤더를 보낸 뒤 끊기므로 클라이언트는 오류로 본다). 펌프가 죽으면 이미 매 ping마다 하는 Postgres 재조회를 **폴링 모드로 계속** 쓰고, 종료 이벤트나 **재구독 5회 연속 실패**에서만 끝낸다. 재연결마다 ERROR 트레이스백을 남기는 것도 멈춘다(첫 실패에 warning 한 번, 이후 백오프).

**8.3 재시도 후 스트림 계약.** 종료 이벤트를 만나면 그 시점의 `runs.status`를 한 번 확인하고, 이미 종료가 아니면(재시도로 다시 돌고 있으면) 계속 흘린다. 클라이언트에게 재연결 책임을 넘기지 않는다.

**8.4 렌더링을 노드 타임아웃 안으로.** `_rendered` 호출을 `asyncio.timeout(timeout)` 안으로 옮기고, 렌더 대기열을 세마포어로 묶어 밀리면 늘어지는 대신 빠르게 실패하게 한다. 지금은 `RENDER_POOL_SIZE`가 워커의 실질 동시성 한계인데 그 사실이 설정 어디에도 드러나지 않는다. 함께 `api/main.py`가 풀 크기를 라이브러리 기본값으로 두고 워커만 계산하는 비대칭도 맞춘다.

---

## 9. 보안 태세 변경

- `ENGINE_API_TOKEN`이 없으면 기동 거부(`ENGINE_DEV_INSECURE=1` 예외).
- `ENGINE_SECRET_KEY`도 같은 규칙으로 필수.
- `runs.inputs`/`runs.outputs`를 `bytea` 암호문으로. 쓰기 경로에서 암호화, 읽기 경로에서 복호화 후 레닥션.
- 시크릿 값 최소 8자.

무엇이 암호화되고 무엇이 평문 메타데이터인지:

| | 저장 형태 |
|---|---|
| `runs.inputs`, `runs.outputs` | AES 암호문(`bytea`) |
| 체크포인트 | AES 암호문(2a, `LANGGRAPH_AES_KEY`) |
| `secrets.ciphertext` | AES-GCM(`ENGINE_SECRET_KEY`) |
| `node_runs.input` 안의 시크릿 필드 | 시크릿 마커 문자열 — 애초에 값이 아니다 |
| `node_runs.input/output`의 나머지, `run_events.payload` | 평문(레닥션·클립 후) |
| `runs.status`, 시각, 토큰 수, 오류 코드 | 평문 |

`0002` 마이그레이션은 `secrets` 테이블, `runs.purged_at`, `runs.inputs/outputs`의 `jsonb → bytea` 전환을 담는다. PR #1·#2가 아직 머지되지 않아 운영 데이터가 없으므로 열을 **옮기지 않고 버리고 다시 만든다**.

---

## 10. 배포

`deploy/docker-compose.yml` — postgres, redis, api, worker. 엔진 이미지는 `services/engine/Dockerfile`(uv 멀티스테이지, non-root 실행).

- **Ollama는 번들하지 않는다.** 온프레미스 GPU 호스트에 이미 있는 것을 `OLLAMA_BASE_URL`로 가리킨다.
- `.env.example`에 필수 변수와 키 두 개(`LANGGRAPH_AES_KEY`, `ENGINE_SECRET_KEY`)를 만드는 방법을 적는다.
- API 헬스체크는 토큰을 실어야 한다 — 2a가 `/healthz`를 토큰 뒤에 뒀다.
- 마이그레이션은 api·worker 양쪽 다 기동 시 수행하고, 2a Task 17이 넣은 어드바이저리 락이 경합을 막는다.
- 워커는 `replicas`로 늘릴 수 있어야 한다(리스와 reaper가 이미 그것을 전제로 만들어졌다).

---

## 11. 테스트

### 11.1 Task 0 스파이크 — TLS 핀 고정

나머지를 쌓기 전에 실제 TLS 서버로 증명한다. 실패하면 §5.4를 먼저 고친다.

1. 검사한 IP로 연결하면서 `Host`·SNI·인증서 검증이 **원래 이름** 기준으로 동작한다.
2. 이름이 맞지 않는 인증서를 내주는 서버에는 **연결이 실패한다**(검증이 살아 있다는 증거).
3. 리다이렉트 홉에서도 같은 성질이 유지된다.

인증서는 `trustme`(개발 의존성, 테스트 CA 발급 전용)로 만든다.

### 11.2 인수 인바리언트

Egress — 리졸버를 주입 가능한 포트로 두고 **실제 DNS를 쓰지 않는다**:

```
allowlist에 없는 호스트    → DNS 조회 자체가 없음 → HTTP_BLOCKED
허용 호스트, A/AAAA 중 하나가 금지 IP → HTTP_BLOCKED
허용 호스트, 모든 IP 공인  → 검사한 그 IP로 연결
리다이렉트 → 사설 IP       → HTTP_BLOCKED
리다이렉트 → 미허용 호스트 → HTTP_BLOCKED
https → http 리다이렉트    → HTTP_BLOCKED
*.example.com              → example.com 자체는 매칭하지 않음
http://host:443 항목       → 기동 거부
::ffff:127.0.0.1           → HTTP_BLOCKED
allowPrivate 항목          → 다른 항목으로 매칭된 요청에는 적용되지 않음
```

Secret:

```
URL·헤더·바디의 시크릿     → 요청 성공, 기록된 요청·출력에 평문 없음
응답이 시크릿을 되돌려줌   → 저장된 출력은 [REDACTED]
시크릿이 든 URL에서 예외   → 기록된 오류·로그에 평문 없음
겹치는 시크릿              → 긴 것부터 치환
8자 미만 시크릿            → API가 거부
```

"평문이 없다"는 DB 행·이벤트·API 응답·`caplog` **네 곳에서 동시에** 주장한다.

Retention:

```
최근 실행    → 페이로드 유지
기한 지난 실행 → 메타데이터 유지, 페이로드 제거, 체크포인트 제거, purged_at 설정
기한 지난 실행 → retry → RUN_DATA_EXPIRED
워크플로 삭제 → 고아 체크포인트 없음
```

하드닝:

```
fan-out 한도까지 채운 워커 → 정상 실행이 리스를 잃지 않음
하트비트가 lease_sec 넘게 실패 → 스스로 실행을 멈춤
Redis 정지 중 SSE          → 폴링으로 계속, 종료 이벤트에서 끝남
재시도된 실행의 스트림      → 재연결 없이 이어짐
렌더 대기열 포화           → 노드 타임아웃으로 실패(실행이 늘어지지 않음)
```

---

## 12. 열린 위험

| 위험 | 대응 |
|---|---|
| **TLS 핀 고정이 라이브러리에서 보장되지 않음** — §5의 보안 모델이 코드에서 무너진다 | Task 0 스파이크로 먼저 증명. 실패 시 대안: 직접 소켓 연결 후 `ssl_context.wrap_socket(server_hostname=...)`으로 감싸 넘기기, 최후에는 egress 전용 프록시 프로세스 |
| `loop.getaddrinfo`가 executor 스레드를 쓴다 — `analyze`, 렌더 풀 종료와 같은 풀을 공유 | 동시 해석 수를 세마포어로 묶는다 |
| `runs.inputs` 암호화 후 SQL로 실행 입력을 들여다볼 수 없다 | 운영 절차가 API를 거치도록 README에 명시 |
| 토큰 기동 거부가 기존 개발 환경·테스트 픽스처를 깨뜨린다 | 픽스처를 함께 고치고 README에 이전 안내를 적는다 |
| 8자 최소 길이가 짧은 레거시 API 키를 거부한다 | 감수하고 문서에 남긴다. 값 기반 레닥션의 안전장치가 그 대가다 |
| 값 기반 레닥션이 노드 경계에서만 돌아 MVP 문서 10.3의 문면과 다르다 | §2 표에 근거를 적어둔다. 하류로 흐를 경로가 없다는 것이 논거다 |

---

## 부록: Plan 3 이후로 넘기는 것

- 실행 기록 내보내기(감사 용도)
- 계정·권한과 워크스페이스 분리 — `secrets.workspace_id`는 자리만 잡아둔다
- 자동 PII 탐지(MVP 비목표 2.2)
- `http_request`의 바이너리 응답 처리
