# 웹 에디터(Plan 3) 설계

MVP 설계: `docs/superpowers/specs/2026-09-11-agentic-workflow-builder-mvp-design.md` (이하 **MVP 문서**)
선행: Plan 1 엔진 코어(PR #1), Plan 2a 런타임 코어(PR #2), Plan 2b HTTP·보안·운영(PR #4)

이 문서는 MVP 문서의 하위 프로젝트 3(웹 에디터)을 다룬다. Plan 1·2가 만든 것은 API 뒤에 있고 사람이 쓸 수 있는 표면이 없다. Plan 3은 그 표면을 만든다.

MVP 문서가 이미 정한 내용은 반복하지 않고 **구현 수준의 결정과 MVP 문서와 달라지는 점만** 적는다.

---

## 1. 목표와 완료 기준

브라우저만으로 다음이 되면 Plan 3은 끝난 것이다.

1. 팔레트에서 노드를 끌어다 놓고, 핸들끼리 잇고, 설정을 채우고, 자동 정렬하고, 되돌린다.
2. 저장하지 않아도 1초 뒤 저장되고, 다른 탭이 먼저 저장했으면 덮어쓸지 불러올지 고른다.
3. 검증 오류가 노드·엣지에 배지로 붙고, 오류가 하나라도 있으면 실행 버튼이 눌리지 않는다.
4. 실행 버튼을 누르면 Start 입력 폼이 뜨고, 실행 중 노드 색이 SSE로 바뀌고, LLM 토큰이 흘러나온다.
5. 승인 대기에 걸린 실행을 승인·반려하고, 실행 중인 것을 취소하고, 노드를 눌러 시도별 입출력을 본다.
6. 새로고침해도 실행 상태가 복원되고, 네트워크가 끊겼다 붙으면 놓친 이벤트가 채워진다.
7. `docker compose -f deploy/docker-compose.yml up`에 web이 함께 뜨고, Playwright E2E(MVP 문서 12장)가 그 스택을 상대로 통과한다.

MVP 문서 2.3의 성공 기준 중 "비개발자가 워크플로를 만들고 돌려본다"가 여기에 대응한다.

**Plan 3 범위**: 캔버스, 노드 패널(설정·정책·라벨), 템플릿 입력과 자동완성, 스키마 에디터, 검증 표시, 자동 저장, 테스트 실행과 SSE, 실행 기록 패널, 승인 UI, 취소, BFF 프록시, web 컨테이너, E2E.

**Plan 3 비범위**: 계정·권한(공유 토큰 그대로), 다중 워크스페이스, AI 코파일럿(자연어 → DSL), RAG 지식 베이스, 실행 기록 내보내기, 워크플로 버전 비교·롤백 UI, 모바일 레이아웃.

**코드 배치**

```
apps/web/
  app/                     Next.js App Router
    api/engine/[...path]/  BFF 프록시 (토큰을 붙이는 유일한 곳)
    workflows/[id]/        에디터 화면
  components/              캔버스, 패널, 대화상자
  lib/engine/              API 타입과 클라이언트 (서버 전용)
  lib/dsl/                 DSL 문서 모델, 커맨드 스택, ID 생성  ← 순수 TS
  lib/template/            참조 파서, 자동완성 소스             ← 순수 TS
  store/                   Zustand 슬라이스
  e2e/                     Playwright
deploy/docker-compose.yml  web 서비스 추가
services/engine/           §2의 엔진 변경 2건
```

---

## 2. MVP 문서에서 달라지는 점

| MVP 문서 | Plan 3 | 이유 |
|---|---|---|
| 9장: 브라우저가 엔진 API를 직접 호출 | **BFF 프록시를 통해서만 호출**한다. 브라우저는 `ENGINE_API_TOKEN`을 절대 받지 않는다 | §3.2. 토큰은 모든 경로에 걸린 공유 비밀이고, 브라우저에 두면 그대로 유출된다. 게다가 `EventSource`는 헤더를 붙일 수 없어 SSE는 직접 호출이 **원리적으로 불가능**하다 |
| 9장: `/validate` 결과로 배지 표시 | `/validate` 응답에 **노드별 `variables`·`outputSchema`·`handles`를 추가**한다 | §4.3. 자동완성에 필요한 "이 노드에서 쓸 수 있는 변수"는 엔진의 `compute_before`가 이미 계산하지만 API로 나오지 않는다. 에디터가 같은 규칙을 다시 구현하면 두 벌이 갈라진다 |
| 4.7: 노드 category | `http_request`의 `"action"`을 **`"Action"`으로 정규화**한다 | §4.4. 나머지 8종은 `IO`/`Logic`/`AI`/`Human`인데 이것만 소문자라 팔레트에 그룹이 하나 더 생긴다 |
| 9장: RJSF로 설정 탭 | RJSF를 쓰되 **`x-template` 필드와 스키마 필드는 커스텀 위젯**으로 뺀다 | §5.2. 템플릿 입력과 스키마 에디터는 RJSF의 기본 위젯으로 만들 수 없다 |
| 12장: E2E는 Playwright | **실제 compose 스택**을 상대로 돌린다. API 목은 쓰지 않는다 | §8.2. Plan 1·2가 목 없이 검증된 이유와 같다. 목은 계약이 갈라진 것을 못 잡는다 |

---

## 3. 아키텍처

### 3.1 구성

```
브라우저 ──┐
           │  같은 오리진 (/api/engine/*)
           ▼
     Next.js 서버 (apps/web)
           │  Authorization: Bearer $ENGINE_API_TOKEN
           ▼
     엔진 API (services/engine)
```

- **Next.js 15 App Router**, React 19, TypeScript strict, pnpm, Node 22.
- 에디터 화면은 클라이언트 컴포넌트다. 캔버스 상태가 전부 클라이언트에 있으므로 서버 컴포넌트로 쪼갤 이득이 없다. 서버 컴포넌트는 첫 로드의 워크플로 목록·상세에만 쓴다.
- 상태는 **Zustand** 한 스토어의 슬라이스 네 개: `graph`(DSL 문서·선택·undo 스택), `validation`(마지막 분석 결과), `save`(revision·저장 상태), `run`(실행 상태·노드 상태·이벤트).

### 3.2 BFF 프록시 — 왜 필수인가

Plan 2b가 만든 인증은 `TokenAuthMiddleware`다. 라우팅 앞에 서서 **모든 경로**(`/healthz`와 `/openapi.json` 포함)에 `Authorization: Bearer <ENGINE_API_TOKEN>`을 요구한다. 그리고 앱에는 **CORS 미들웨어가 없다**.

여기서 세 가지가 따라온다.

1. 브라우저가 엔진을 직접 부르면 프리플라이트부터 막힌다. CORS를 열려면 엔진에 미들웨어를 추가해야 하는데, 그건 공유 토큰 하나로 지켜지는 표면을 넓히는 일이다.
2. 토큰을 브라우저에 내려보내면 공유 비밀이 아니게 된다. MVP에 계정이 없으므로 이 토큰 하나가 전부다.
3. **`EventSource`는 헤더를 설정할 수 없다.** 쿼리 파라미터로 토큰을 넘기는 우회는 URL을 프록시 로그·브라우저 기록에 남기므로 Plan 2b의 로그 정책(MVP 문서 10.1: URL은 host+path만)과 정면으로 충돌한다.

따라서 **프록시가 유일하게 올바른 답**이다. `app/api/engine/[...path]/route.ts`가 요청을 받아 토큰을 붙여 엔진으로 넘기고 응답을 그대로 돌려준다.

- 오류 봉투(`{error: {code, message, details}}`)는 **가공하지 않고 상태 코드와 함께 그대로** 전달한다. 에디터가 `REVISION_CONFLICT`의 `details.currentRevision`을 읽어야 하기 때문이다.
- SSE는 응답 본문을 **버퍼링 없이 흘려보낸다**. Next.js Route Handler가 `ReadableStream`을 그대로 반환하면 되지만, 실제로 청크가 즉시 나가는지는 런타임·빌드 모드에 따라 다르므로 **Task 0 스파이크로 먼저 증명한다**.
- `Last-Event-ID` 요청 헤더를 그대로 전달한다. 이게 빠지면 재연결이 이벤트를 처음부터 다시 받는다.
- 프록시는 **경로를 허용 목록으로 제한한다**. `/workflows`, `/runs`, `/node-types`, `/secrets`, `/healthz`만 통과시킨다. `/openapi.json`과 `/docs`는 막는다 — 브라우저에서 열 이유가 없고, 프록시가 열어주면 인증 없는 공개 문서가 된다.
- 프록시는 **시크릿 값을 읽는 경로가 없다**는 사실에 기대지 않는다. Plan 2b의 `/secrets`는 쓰기 전용이므로 값이 돌아올 길이 애초에 없지만, 프록시는 응답 본문을 해석하지 않으므로 이 성질은 엔진 쪽 불변식으로 남는다.

### 3.3 토큰과 환경 변수

| 변수 | 어디에 | 설명 |
|---|---|---|
| `ENGINE_API_URL` | web 서버 전용 | 컨테이너 안에서는 `http://api:8000` |
| `ENGINE_API_TOKEN` | web 서버 전용 | `NEXT_PUBLIC_` 접두사를 **절대 붙이지 않는다** |

`lib/engine/*`는 `import "server-only"`로 시작한다. 클라이언트 번들에 딸려 들어가면 빌드가 실패하도록 만드는 것이 주석보다 낫다.

---

## 4. 엔진에 가하는 변경 2건

Plan 3은 원칙적으로 프런트엔드 작업이지만, 에디터가 없으면 드러나지 않았던 API 공백 두 개를 엔진에서 메운다.

### 4.1 `/validate` 응답 확장

지금:

```json
{ "issues": [ { "severity": "error", "code": "...", "message": "...", "nodeId": "llm_1" } ] }
```

바꾼 뒤:

```json
{
  "issues": [ ... ],
  "nodes": {
    "llm_1": {
      "variables": ["start", "template_1"],
      "outputSchema": { "type": "object", "properties": { "text": { "type": "string" } } },
      "handles": ["out"]
    }
  }
}
```

- `variables`: `compute_before(graph)`의 결과. "이 노드 시점에 **반드시** 실행이 끝나 있는 노드"들이다. 자동완성은 이 집합만 제안한다.
- `outputSchema`: `compute_schemas(graph)`의 결과. `{{ llm_1.text }}`까지 제안하고 타입 불일치를 인라인 표시하는 근거다.
- `handles`: `spec.handles(config)`. 분기 노드의 출력 핸들 이름은 **설정에 따라 달라지므로**(classifier는 카테고리 id들, condition은 `true`/`false`, human_approval은 `approve`/`reject`) `/node-types`로는 알 수 없다.
- `nodes`는 **phase 1·2를 통과했을 때만** 채워진다. `analyze()`가 오류가 난 단계에서 멈추므로 `Analysis.graph`가 `None`이면 `"nodes": {}`를 돌려준다. 에디터는 그럴 때 **마지막으로 성공한 분석 결과**를 계속 쓴다 — 구조가 깨진 순간에 자동완성이 통째로 사라지면 고치기가 더 어렵다.

응답 크기는 `outputSchema` 때문에 커질 수 있다. 노드 15,000개 한도를 생각하면 상한이 필요하다: `nodes`는 `MAX_ISSUES`와 같은 이유로 **노드 200개까지만** 채우고, 넘으면 `"nodesTruncated": true`를 붙인다. 그보다 큰 워크플로를 캔버스에서 편집하는 일은 없다.

### 4.2 `http_request` category 정규화

`HttpRequestNode.category = "action"` → `"Action"`. 팔레트는 category로 묶으므로 이대로면 그룹이 `IO`/`Logic`/`AI`/`Human`/`action` 다섯 개가 된다. 엔진 쪽 테스트에 category 문자열을 박아둔 곳이 있으면 함께 고친다.

---

## 5. 캔버스와 노드 패널

### 5.1 편집 모델 — React Flow가 아니라 DSL이 원본

React Flow의 내부 노드·엣지 배열을 원본으로 삼으면 되돌리기와 자동 저장이 둘 다 어려워진다. 원본은 **DSL 문서**(`WorkflowDSL`과 같은 모양의 평범한 JS 객체)이고, React Flow에 넘기는 것은 거기서 파생한 뷰다.

- 편집은 전부 **커맨드**를 거친다: `addNode`, `removeNode`, `connect`, `disconnect`, `setConfig`, `setLabel`, `setPolicy`, `setPosition`, `autoLayout`. 커맨드는 `(dsl) => dsl`인 순수 함수다.
- undo/redo는 커맨드 스택이다. **적용 전 문서를 통째로 보관**한다 — 역연산을 따로 구현하지 않는다. DSL은 512KB 이하이고 스택은 50단계로 제한하므로 최악 25MB, 실제로는 훨씬 작다. 역연산 구현이 틀리는 쪽이 메모리보다 비싸다.
- `setPosition`은 드래그 중 초당 수십 번 일어난다. **드래그가 끝날 때 한 번만** 스택에 쌓고, 연속된 `setPosition`은 병합한다.
- `position`과 `label`은 `dsl_hash` 계산에서 빠지므로(엔진 `dsl_hash`의 docstring) 위치만 옮긴 저장은 새 버전을 만들지 않는다. 그래도 `revision`은 올라가고 자동 저장은 일어난다.

### 5.2 노드 ID

MVP 문서 4.2: `<type>_<n>`, 한 번 정해지면 불변. `n`은 **그 타입의 기존 최대 번호 + 1**이다. 삭제된 번호를 재사용하지 않는다 — 재사용하면 실행 기록의 `node_runs.node_id`가 지워진 노드의 것과 겹쳐 읽힌다. `start`와 `end`는 번호 없이 하나씩만 둔다.

### 5.3 설정 탭

`/node-types`의 `configSchema`를 RJSF(`@rjsf/core` + `@rjsf/validator-ajv8`)에 넘긴다. 위젯은 세 갈래로 나뉜다.

| 스키마 | 위젯 |
|---|---|
| `x-template: true` | §6의 템플릿 편집기 |
| `start.inputs`, `llm.outputSchema` | §7의 스키마 에디터 |
| 나머지 | RJSF 기본 |

`classifier.categories`(배열)와 `condition.conditions`(배열)는 기본 위젯으로 되지만 라벨이 영어 필드명 그대로 나오므로 `uiSchema`로 한국어 라벨을 준다. `uiSchema`는 노드 타입별 상수 테이블이다 — 엔진이 라벨을 주지 않으므로 여기서만 산다.

### 5.4 실행 정책 탭

`defaultPolicy`가 `null`이 아닌 노드(llm, classifier, http_request)에만 보인다. 필드는 `timeoutSec`, `retry.maxAttempts`, `retry.backoff`, `retry.initialDelaySec`, `onError`, `defaultOutput`.

- 비워두면 DSL에 `policy`를 **쓰지 않는다**. 빈 객체 `{}`를 쓰면 안 된다: 엔진 로드맵이 기록한 대로 `dsl_hash`가 `policy: {}`와 policy 없음을 다르게 해싱하므로, 의미 없는 새 버전이 생긴다.
- `http_request`의 POST·PATCH는 엔진이 `maxAttempts=1`을 강제한다(`policy_for`). 정책 탭은 이때 **"POST·PATCH는 재시도하지 않습니다"**를 안내로 띄우고 `maxAttempts` 입력을 비활성화한다. 사용자가 3을 넣어도 엔진이 1로 돌린다는 사실을 화면에서 숨기지 않는다.

---

## 6. 템플릿 입력

MVP 문서 9장이 요구하는 것: `{{` 입력 시 자동완성, 참조를 라벨 칩으로 렌더링, 타입 불일치 인라인 표시.

### 6.1 편집기 선택

**CodeMirror 6**(`@codemirror/view`, `@codemirror/state`, `@codemirror/autocomplete`)을 쓴다. `textarea` + 오버레이로 칩을 흉내내는 방법은 줄바꿈·IME·스크롤 동기화에서 반드시 깨진다 — 한국어 입력이 주 사용례이므로 IME는 타협할 수 없다.

- 자동완성 소스는 `validation` 슬라이스의 `nodes[thisNode].variables`와 각 후보의 `outputSchema`에서 만든다. `{{ ` 다음에 노드 id, `.` 다음에 그 노드 출력 스키마의 프로퍼티를 제안한다.
- 제안 라벨은 **노드 라벨**을, 삽입 문자열은 **노드 id**를 쓴다. 사람은 "요약"을 고르고 문서에는 `template_1`이 들어간다.
- `{{ ... }}` 범위에 `Decoration.replace`로 칩 위젯을 얹는다. 커서가 그 범위 안에 있으면 칩을 풀어 원문을 보여준다 — 편집할 수 없는 칩은 고칠 수가 없다.
- **보장 집합에 없는 노드**도 제안은 하되 회색으로 "기본값 필요"를 붙인다. MVP 문서 9장이 요구한 표시다. 엔진은 이것을 `REF_NOT_GUARANTEED` 경고로 잡는다.

### 6.2 비개발자용 안내

엔진의 템플릿 규칙은 Jinja2와 다르다. 편집기 하단에 접이식 도움말을 두고 다음을 적는다(로드맵 Plan 3 체크리스트에서 그대로 가져온다).

- 값을 이으려면 `{{ a }}{{ b }}`처럼 나란히 쓴다. `~`와 문자열 `+`는 거부된다.
- 산술은 숫자끼리만 된다.
- 반복문은 중첩할 수 없다. 중첩 깊이는 50까지.
- 렌더된 필드 하나는 최대 1,000,000자.
- `default(x)`는 **없는 값**만 바꾼다. `null`도 없는 값으로 치려면 `default(x, true)`.
- JSON 형식 템플릿에서는 `{{ }}` 하나가 JSON 값 하나다. `{"name": {{ start.name }}}`처럼 **따옴표 없이, `| tojson` 없이** 쓴다. `tojson`을 붙이면 두 번 인코딩된다.
- 값의 중첩은 100단계까지.

이 문단은 엔진 규칙의 사본이므로 **엔진이 규칙을 바꾸면 여기도 바꿔야 한다**. Task 22에서 `services/engine/README.md`와 서로 링크를 건다.

---

## 7. 스키마 에디터

`start.inputs`와 `llm.outputSchema`는 JSON Schema지만, 엔진이 받아들이는 것은 부분집합이다(`engine.jsondata.schema_problems`가 통과시키는 키워드): `type`, `properties`, `required`, `additionalProperties`, `items`, `anyOf`/`oneOf`, `enum`/`const`, 수치·길이·개수 범위(≤ 10,000), 주석 계열.

에디터는 **폼**으로 제공한다. 필드 목록(이름, 타입, 필수 여부, 설명)을 추가·삭제하는 표이고, 중첩 객체와 배열은 한 단계씩 들어간다. 원문 JSON 편집 모드를 함께 두되 기본은 폼이다 — 대상 사용자가 JSON Schema를 아는 사람이 아니다.

검증은 `/validate`에 맡긴다. 에디터가 `schema_problems`를 다시 구현하지 않는다. 대신 응답의 `field`가 가리키는 위치에 메시지를 인라인으로 붙인다.

---

## 8. 실행

### 8.1 시작과 구독

1. 실행 버튼 → Start 노드의 `inputs` 스키마로 만든 폼.
2. `POST /workflows/{id}/runs`에 **요청마다 새로 만든 UUID**를 `Idempotency-Key`로 붙인다. 더블 클릭 방지는 이것으로 한다.
3. 202 응답의 `runId`로 `EventSource("/api/engine/runs/{id}/events")`를 연다.

`EventSource`는 끊기면 스스로 재연결하고 마지막 `id:`를 `Last-Event-ID`로 보낸다. 프록시가 그 헤더를 전달하므로(§3.2) 엔진이 그 지점부터 다시 보낸다. **에디터는 재연결 로직을 직접 쓰지 않는다** — `EventSource`가 이미 하는 일이다.

다만 `EventSource`는 **HTTP 오류에서도 재연결을 계속 시도한다**. 실행이 이미 끝나 스트림이 정상 종료된 경우와, 프록시가 502를 돌려주는 경우를 구분해야 한다. 종료 이벤트(`run_succeeded`/`run_failed`/`run_cancelled`)를 받으면 **에디터가 명시적으로 `close()`** 한다. 그러지 않으면 끝난 실행에 대해 영원히 재연결한다.

### 8.2 노드 상태

노드 색은 이벤트로 갱신한다: `node_started` → 실행 중, `node_finished` → 성공/기본값, `node_failed` → 실패, `node_waiting` → 승인 대기. `attempt`가 1보다 크면 재시도 표시.

`node_token`은 `seq`가 없는 임시 이벤트다(설계 7.3). 토큰 미리보기에만 쓰고 **상태 판단에 쓰지 않는다** — 유실될 수 있다.

### 8.3 새로고침 복원

새로고침하면 `EventSource`의 `Last-Event-ID`가 사라진다. 복원 절차는:

1. `GET /runs/{id}` — 현재 상태.
2. `GET /runs/{id}/nodes` — 지금까지의 노드 실행 기록. 노드 색을 여기서 복원한다.
3. 실행이 아직 진행 중이면 `EventSource`를 처음부터 연다. 엔진이 저장된 이벤트를 전부 재생하고 라이브로 넘어간다.

실행 id는 `sessionStorage`가 아니라 **URL**(`?run=<id>`)에 둔다. 새로고침·뒤로가기·링크 공유가 전부 공짜로 된다.

### 8.4 승인과 취소

- `waiting` 상태의 실행은 `GET /runs/{id}`의 `waitingFor`(`{nodeId, execIndex, message, review, allowEdit}`)로 대화상자를 만든다. `allowEdit`이 참일 때만 값 편집을 연다.
- `POST /runs/{id}/resume` 본문은 `{nodeId, execIndex, decision, comment?, editedValue?}`. **`reviewedAt`은 보내지 않는다** — 엔진이 서버 시각으로 채운다.
- 취소는 `POST /runs/{id}/cancel`. 응답이 `cancelled`면 즉시 반영하고, 그렇지 않으면 **"취소 중"**을 표시한 뒤 `run_cancelled` 이벤트로 확정한다.

### 8.5 레닥션된 값

`node_runs`의 값에는 `[REDACTED]` 문자열이 들어 있다. 기록 패널은 이것을 **자물쇠 아이콘과 회색 배지**로 렌더링한다. 평범한 문자열처럼 보이면 사용자가 자기 설정이 잘못됐다고 오해한다.

---

## 9. 자동 저장과 충돌

- 변경 후 1초 디바운스. 저장 중에 또 바뀌면 저장이 끝난 뒤 한 번 더 보낸다(합쳐서 한 번).
- `PUT /workflows/{id}`에 현재 `revision`을 싣고, 200이면 새 `revision`으로 갱신한다.
- 409 `REVISION_CONFLICT`면 대화상자: **"다른 곳에서 변경됨"** — [불러오기] 응답의 `details.draftDsl`로 교체, [내 변경으로 덮어쓰기] `details.currentRevision`으로 재전송.
- 덮어쓰기는 **한 번만** 자동 재시도한다. 두 번째 409는 사용자에게 다시 묻는다. 무한 재시도는 두 탭이 서로를 덮어쓰는 경쟁이 된다.
- 저장 실패(네트워크, 5xx)는 상태 표시줄에 남기고 다음 변경에 다시 시도한다. **편집 내용을 버리지 않는다.**

---

## 10. 검증 표시와 한국어화

- 저장 성공 후 `POST /workflows/{id}/validate`를 부른다. 저장과 같은 디바운스를 공유한다.
- `issue.nodeId` → 노드 배지, `issue.edgeId` → 엣지 배지, `issue.field` → 패널 안 해당 입력 옆.
- `severity: "error"`가 하나라도 있으면 실행 버튼 비활성.
- `LIMIT_EXCEEDED`는 노드에 붙지 않고 화면 상단 배너로 뜬다. 엔진이 한도 초과 시 노드별 issue를 만들지 않기 때문이다.
- **pydantic 메시지 한국어화**: 설정·정책 오류는 한국어 접두사 뒤에 pydantic의 영어 `msg`가 붙어 온다. `issue.code`와 `field`만으로는 부족하므로, 에디터가 영어 꼬리표를 pydantic 오류 타입별 한국어 문장으로 바꾸는 매핑 테이블을 갖는다. 매핑에 없으면 원문을 그대로 보여준다 — 영어라도 보이는 편이 빈 메시지보다 낫다.

---

## 11. 테스트

| 수준 | 대상 | 방법 |
|---|---|---|
| 단위(vitest) | `lib/dsl` | 커맨드 순수성, ID 생성, undo/redo, 위치 병합 |
| 단위(vitest) | `lib/template` | `{{` 파싱, 자동완성 후보 생성, 칩 범위 계산 |
| 단위(vitest) | 오류 한국어화 | pydantic 타입별 매핑, 미매핑 폴백 |
| 컴포넌트(vitest + testing-library) | 패널, 대화상자 | 충돌 대화상자의 두 선택지, 승인 대화상자의 `allowEdit` |
| 라우트(vitest) | BFF 프록시 | 토큰 주입, 경로 허용 목록, 오류 봉투 통과, `Last-Event-ID` 전달 |
| E2E(Playwright) | 전체 | **실제 compose 스택**. MVP 문서 12장의 다섯 시나리오 |

E2E 시나리오(MVP 문서 12장):

1. 노드 배치 → 연결 → 설정 → 실행 → 결과 확인
2. 승인 흐름(대기 → 승인 → 완료)
3. 두 탭 충돌 대화상자
4. 새로고침 후 실행 상태 복원
5. 검증 오류가 있으면 실행 버튼 비활성

E2E에서 LLM은 문제다. 온프렘 Ollama를 CI에 둘 수 없다. **해결**: E2E 워크플로는 `template`·`condition`·`human_approval`·`http_request`만 쓴다. `http_request`는 web 컨테이너 자신의 `/api/health`를 부른다 — allowlist에 넣을 수 있고, 외부 의존이 없고, 실제 소켓을 쓴다. LLM 노드의 UI는 컴포넌트 테스트가 덮는다.

---

## 12. 열린 위험

| 위험 | 대응 |
|---|---|
| **Next.js Route Handler가 SSE를 버퍼링한다** — 실행 화면이 통째로 동작하지 않는다 | Task 0 스파이크로 먼저 증명. 실패 시 대안: Node 런타임 강제(`export const runtime = "nodejs"`)와 `dynamic = "force-dynamic"`, 그래도 안 되면 프록시를 별도 경량 서버로 분리 |
| CodeMirror 6 + 한국어 IME + 칩 데코레이션이 충돌한다 | Task 10에서 IME 입력을 명시적으로 테스트한다. 실패 시 칩 렌더링을 포기하고 구문 강조만 남긴다 — 자동완성이 칩보다 중요하다 |
| `/validate` 응답의 `outputSchema`가 커서 자동 저장 주기마다 수백 KB가 오간다 | 노드 200개 상한(§4.1)과 디바운스 공유. 그래도 크면 `outputSchema`를 요청 파라미터로 선택적으로 만든다 |
| RJSF의 번들 크기 | 에디터 화면에서만 동적 import 한다 |
| E2E가 compose 스택에 의존해 CI에서 느리다 | E2E를 별도 잡으로 분리하고 단위·컴포넌트는 Docker 없이 돈다 — Plan 2의 `-m "not integration"`과 같은 원칙 |
| 공유 토큰 하나라 여러 사람이 같은 워크플로를 동시에 편집한다 | 낙관적 잠금이 데이터 손실은 막지만 경험은 나쁘다. 계정이 생기기 전까지 감수하고 README에 적는다 |

---

## 부록: Plan 3 이후로 넘기는 것

- 계정·권한과 워크스페이스 분리
- AI 코파일럿(자연어 → DSL)
- RAG 지식 베이스 노드
- 워크플로 버전 비교·롤백 UI
- 실행 기록 내보내기(감사 용도)
- 모바일·태블릿 레이아웃
- 실시간 공동 편집(CRDT)
