# 에이전틱 워크플로우 빌더 — MVP(코어 + 에디터) 설계

- 작성일: 2026-09-11
- 상태: 설계 승인됨 (구현 계획 작성 전)
- 범위: 하위 프로젝트 1(워크플로우 코어) + 2(비주얼 에디터)

---

## 1. 배경: 조사 요약

### 1.1 워크플로우 vs 에이전트 (Anthropic, *Building Effective Agents*)
- **워크플로우**: 미리 정의된 코드 경로로 LLM·도구를 오케스트레이션. 예측 가능·디버깅 쉬움.
- **에이전트**: LLM이 스스로 프로세스와 도구 사용을 결정. 유연하지만 비용·오류 누적 위험.
- 조합 가능한 5가지 패턴: Prompt Chaining, Routing, Parallelization, Orchestrator-Workers, Evaluator-Optimizer.
- 권고: 단순한 조합에서 시작하고, 도구 설명(ACI)에 UI만큼 투자할 것.

### 1.2 기존 구현체
| 부류 | 예 | 특징 |
|---|---|---|
| 코드 우선 | LangGraph | StateGraph + 조건부 엣지 + checkpointer, interrupt 기반 HITL |
| 비주얼 빌더 | Dify, Langflow, Flowise, n8n, Coze | 드래그앤드롭 캔버스, Dify는 ReactFlow 유사 그래프를 YAML DSL로 export |
| 내구 실행 | Temporal, Inngest | 스텝 단위 체크포인트·재생, 장기 대기(HITL) |

공통 레이어: 에디터 UI → 워크플로우 정의(DSL) → 검증기 → 실행 엔진 → 내구성/HITL → 관측성.

### 1.3 출처
- [Building Effective AI Agents — Anthropic](https://www.anthropic.com/research/building-effective-agents)
- [Open Source AI Agent Platform Comparison (2026) — Jimmy Song](https://jimmysong.io/blog/open-source-ai-agent-workflow-comparison/)
- [LangGraph vs n8n — ZenML](https://www.zenml.io/blog/langgraph-vs-n8n)
- [Langflow vs Flowise vs n8n — Blck Alpaca](https://blckalpaca.at/en/knowledge-base/ai-agents/ai-agent-frameworks-comparison/langflow-vs-flowise-vs-n8n)
- [Workflow Editor template — React Flow](https://reactflow.dev/ui/templates/workflow-editor)
- [LangGraph Interrupts — LangChain Docs](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph Persistence: Checkpointers — AI/TLDR](https://ai-tldr.dev/learn/agent-frameworks/langchain-ecosystem/langgraph-persistence-checkpointers/)
- [Dify Workflow DSL skill (DSL 구조 참고)](https://github.com/yzmw123/dify-workflow-dsl-skill)
- [Durable Execution for AI Agents — Inngest](https://www.inngest.com/blog/durable-execution-key-to-harnessing-ai-agents)
- [Durable Execution meets AI — Temporal](https://temporal.io/blog/durable-execution-meets-ai-why-temporal-is-the-perfect-foundation-for-ai)

---

## 2. 제품 결정 사항

| 항목 | 결정 |
|---|---|
| 대상 | 비개발자용 SaaS |
| 워크플로우 유형 | 범용 (자동화·챗봇·콘텐츠 생성 모두) → 노드 플러그인 구조가 핵심 |
| 실행 엔진 | 접근법 B: TS 프론트엔드 + Python(FastAPI + LangGraph) |
| LLM | 온프레미스 Ollama, 소형 모델(~14B) |
| 인프라 | 컨테이너 자체 운영 (Docker Compose) |
| AI 코파일럿 | MVP 제외. 단, DSL은 LLM이 생성·검증하기 쉬운 JSON Schema 기반으로 설계 |

### 2.1 전체 로드맵(하위 프로젝트)
| # | 하위 프로젝트 | 의존 |
|---|---|---|
| **1** | **워크플로우 코어** — DSL, 검증기, 실행 엔진, 노드 라이브러리 | — |
| **2** | **비주얼 에디터** — 캔버스, 노드 설정 패널, 테스트 실행, 실행 추적 | 1 |
| 3 | 런타임 확장 — 스케줄/웹훅 트리거, 수평 확장, 고급 재시도 | 1 |
| 4 | 플랫폼 — 인증, 워크스페이스(멀티테넌시), 권한, 시크릿 저장소 | — |
| 5 | 배포·연동 — API/웹훅/채팅 위젯 공개, 커넥터, RAG 지식베이스 | 1, 3, 4 |
| 6 | 과금·사용량 | 4 |

이 문서는 1 + 2만 다룬다.

### 2.2 MVP 비목표 (명시적 제외)
- 도구 호출 기반 에이전트 노드, Orchestrator-Workers(동적 Send) — 소형 모델의 도구 호출 불안정성 때문
- RAG 지식베이스, 코드 실행 노드(샌드박스 보안), 웹훅/스케줄 트리거
- 인증·멀티테넌시(단일 기본 워크스페이스로 동작, 단 스키마에 `workspace_id` 선반영)
- AI 코파일럿, 과금

### 2.3 성공 기준
1. 비개발자가 에디터에서 Chaining, Routing, Parallelization, Evaluator-Optimizer 패턴 워크플로우를 드래그앤드롭으로 만들고 실행할 수 있다.
2. 실행 중 노드 진행 상황이 캔버스에 실시간으로 표시되고, 노드별 입력·출력·오류를 확인할 수 있다.
3. Human Approval 노드에서 대기 중인 실행은 워커 재시작 후에도 승인 시 이어서 실행된다.
4. 실패한 실행을 재시도하면 이미 성공한 노드는 다시 실행되지 않는다.
5. 새 노드 타입 추가 시 백엔드에 노드 스펙 하나를 추가하는 것만으로 에디터 팔레트·설정 폼에 나타난다.

---

## 3. 아키텍처

```
[web]    Next.js(App Router) + @xyflow/react + Zustand + ELKjs + RJSF(shadcn 테마)
            │  REST + SSE   (TS 타입은 FastAPI OpenAPI → openapi-typescript로 생성)
[api]    FastAPI
            │  Redis 큐(arq)
[worker] Python — 검증기 → 컴파일러 → LangGraph 실행 → 이벤트 발행
            │
[Postgres]  애플리케이션 테이블 + LangGraph 체크포인트(AsyncPostgresSaver)
[Redis]     작업 큐 · 실행 이벤트 pub/sub · 모델별 동시성 세마포어
[Ollama]    모델 서버 (LLM 게이트웨이를 통해서만 호출)
```

### 3.1 저장소 구조
```
apps/web/                      # Next.js 에디터
services/engine/               # Python 패키지 (api와 worker가 공유)
  engine/api/                  # FastAPI 라우터
  engine/worker/               # arq 워커 엔트리포인트
  engine/dsl/                  # DSL Pydantic 모델
  engine/validator/            # 그래프·변수 검증
  engine/compiler/             # DSL → StateGraph
  engine/nodes/                # 노드 스펙 (노드 타입당 1 모듈) + 레지스트리
  engine/llm/                  # LLM 게이트웨이 (Ollama, OpenAI 호환)
  engine/events/               # 실행 이벤트 발행/구독
  engine/db/                   # SQLAlchemy 모델, Alembic 마이그레이션
deploy/docker-compose.yml      # web, api, worker, postgres, redis, ollama
```

### 3.2 컴포넌트 책임
| 컴포넌트 | 책임 | 의존 |
|---|---|---|
| `dsl` | DSL 구조 정의(Pydantic) 및 JSON Schema export | — |
| `nodes` | 노드 타입별 config/input/output 모델 + `execute()` + 핸들 정의, 레지스트리 | `llm` |
| `validator` | DSL을 받아 오류 목록 반환 (부작용 없음) | `dsl`, `nodes` |
| `compiler` | 검증된 DSL → 컴파일된 LangGraph (버전 해시로 캐시) | `dsl`, `nodes` |
| `llm` | 모델 호출, 구조화 출력, 동시성 제한, 타임아웃·재시도 | Redis, Ollama |
| `worker` | 큐 소비, 그래프 실행, `node_runs` 기록, 이벤트 발행 | 위 전부, Postgres |
| `api` | CRUD, 버전 생성, 검증, 실행/재개/재시도/취소, SSE 중계 | `validator`, DB, Redis |
| `web` | 캔버스 편집, 설정 폼 자동 생성, 실행 추적 UI | `api` |

### 3.3 데이터 모델 (Postgres)
| 테이블 | 주요 컬럼 |
|---|---|
| `workflows` | `id`, `workspace_id`, `name`, `draft_dsl jsonb`, `created_at`, `updated_at` |
| `workflow_versions` | `id`, `workflow_id`, `workspace_id`, `version_no`, `dsl jsonb`, `dsl_hash`, `created_at` — **불변** |
| `runs` | `id`, `workspace_id`, `workflow_version_id`, `status`, `inputs jsonb`, `outputs jsonb`, `error jsonb`, `created_at`, `started_at`, `finished_at` |
| `node_runs` | `id`, `run_id`, `node_id`, `attempt`, `status`, `input jsonb`, `output jsonb`, `error jsonb`, `tokens_in`, `tokens_out`, `started_at`, `finished_at` |
| (LangGraph) | 체크포인트 테이블 — `AsyncPostgresSaver.setup()`이 관리 |

- `runs.status`: `queued | running | waiting | succeeded | failed | cancelled`
- 편집은 `workflows.draft_dsl`에 저장. 실행 시 draft의 해시가 최신 버전과 다르면 새 `workflow_versions` 행을 만들고 그 버전으로 실행한다. 실행은 항상 불변 버전을 참조하므로 편집이 대기 중인 실행에 영향을 주지 않는다.
- MVP는 단일 기본 워크스페이스(`workspace_id` 고정값)로 동작한다.

---

## 4. 워크플로우 DSL과 노드 스펙

### 4.1 DSL 구조
```json
{
  "version": "1",
  "nodes": [
    { "id": "start", "type": "start", "position": {"x": 0, "y": 0},
      "config": { "inputs": { "type": "object", "properties": { "topic": {"type": "string"} }, "required": ["topic"] } } },
    { "id": "draft", "type": "llm", "position": {"x": 300, "y": 0},
      "config": { "model": "qwen2.5:14b", "prompt": "{{start.topic}}에 대한 글을 써줘", "temperature": 0.7 } },
    { "id": "end", "type": "end", "position": {"x": 600, "y": 0},
      "config": { "outputs": { "result": "{{draft.text}}" } } }
  ],
  "edges": [
    { "id": "e1", "source": "start", "sourceHandle": "out", "target": "draft", "maxIterations": null },
    { "id": "e2", "source": "draft", "sourceHandle": "out", "target": "end", "maxIterations": null }
  ]
}
```
- 노드 ID: `^[a-z][a-z0-9_]{0,39}$`, 워크플로우 내 유일. 에디터가 생성하고 사용자가 이름을 바꿀 수 있다(참조 자동 갱신).
- 변수 참조: `{{<node_id>.<output_field>}}` (중첩 필드는 `.`으로). 렌더링은 Jinja2 `SandboxedEnvironment` + `StrictUndefined`.
- `position`은 에디터 전용이며 실행에 영향이 없다. `dsl_hash`는 `position`을 제외하고 계산한다.

### 4.2 노드 스펙 규약 (단일 원천 = Python)
```python
class NodeSpec(Protocol):
    type: str                      # "llm"
    label: str                     # 팔레트 표시 이름
    category: str                  # "AI" | "Logic" | "Integration" | "Human" | "IO"
    Config: type[BaseModel]        # 설정 폼 → JSON Schema
    Output: type[BaseModel]        # 출력 필드 → 변수 참조 검증에 사용
    def handles(self, config) -> list[str]            # 출력 핸들 (분기 노드는 config에 따라 동적)
    async def execute(self, ctx: NodeContext, config) -> Output
```
- `GET /node-types`가 모든 스펙의 `type/label/category/configSchema/outputSchema/handles 규칙`을 반환하고, 에디터는 이것으로 팔레트와 설정 폼(RJSF)을 만든다.
- 노드 추가 = `engine/nodes/<type>.py` 하나 추가 + 레지스트리 등록. 프론트엔드 변경 불필요(커스텀 위젯이 필요한 경우만 예외).

### 4.3 MVP 노드 (9종)
| 타입 | Config 핵심 | Output | 핸들 | 패턴 |
|---|---|---|---|---|
| `start` | 입력 JSON Schema | 입력 필드들 | `out` | — |
| `end` | 출력 매핑(템플릿) | 매핑 결과 | 없음 | — |
| `llm` | model, system, prompt, temperature, `outputSchema?` | `text` 또는 스키마 필드 | `out` | Chaining |
| `classifier` | model, 분류 대상 템플릿, categories[{id, 설명}] | `category`, `reason` | 카테고리별 + `default` | Routing |
| `condition` | 조건식 목록(좌변 템플릿, 연산자, 우변) + AND/OR | `result: bool` | `true`, `false` | Routing / 루프 탈출 |
| `merge` | 합치기 방식(`object` / `list`) | `branches` | `out` | Parallelization |
| `template` | Jinja 템플릿, 출력 형식(text/json) | `text` 또는 파싱된 JSON | `out` | 보조 |
| `http_request` | method, url, headers, body 템플릿, timeout | `status`, `headers`, `body` | `out` | 연동 |
| `human_approval` | 안내 메시지 템플릿, 검토 대상 템플릿, 수정 허용 여부 | `decision`, `comment`, `editedValue` | `approve`, `reject` | HITL |

- `condition` 연산자: `==, !=, >, >=, <, <=, contains, not_contains, is_empty, is_not_empty`. 임의 코드 평가는 하지 않는다.
- `http_request`는 배포 설정의 호스트 allowlist에 있는 대상만 호출한다(SSRF 방지). 기본값은 빈 목록.

### 4.4 그래프 규칙
1. 워크플로우당 `start` 정확히 1개, `end` 정확히 1개. 실행 결과(`runs.outputs`)는 `end`의 출력 매핑 결과다. 분기별로 다른 결과가 필요하면 각 분기가 같은 `end`로 모이고 매핑 템플릿에서 `{% if %}`로 고른다.
2. 모든 노드는 `start`에서 도달 가능하고 `end`에 도달 가능해야 한다.
3. **되돌아가는 엣지(back-edge)** 는 `condition` 또는 `classifier`에서만 출발할 수 있고 `maxIterations`(1–20) 필수. 그 외 사이클은 오류.
4. 한도 소진 시: `condition`은 반대 핸들로, `classifier`는 `default` 핸들로 진행한다.
5. `merge`로 들어오는 모든 경로는 같은 분기 지점에서 무조건 엣지로 갈라진 것이어야 한다(경로 중간에 `condition`/`classifier`가 있으면 오류) — 조건부로 비활성화된 분기를 영원히 기다리는 교착을 막기 위함.
6. 하나의 출력 핸들에서 여러 엣지가 나가면 병렬 실행(fan-out)이다.
7. 같은 fan-out에서 갈라진 둘 이상의 분기가 `merge`가 아닌 노드(`end` 포함)로 합류하면 오류 — 그 노드가 분기 수만큼 중복 실행되기 때문. 병렬 분기는 반드시 `merge`로 합친다. (조건 분기처럼 한 번에 한 경로만 활성화되는 합류는 허용)

---

## 5. 실행 엔진

### 5.1 검증기
`validate(dsl) -> list[ValidationError{code, message, nodeId?, edgeId?, field?}]`
- Pydantic 스키마(노드별 Config 포함), 엣지의 노드·핸들 참조, 4.4의 그래프 규칙, 변수 참조(참조 노드가 모든 경로에서 선행하고 출력 필드가 존재), 템플릿 문법.
- 에디터 저장 시(경고 표시)와 실행 요청 시(오류면 거부) 같은 함수를 사용한다.

### 5.2 컴파일러 (DSL → LangGraph)
- 상태:
  ```python
  class RunState(TypedDict):
      inputs: dict
      outputs: Annotated[dict[str, Any], merge_dicts]    # node_id → output, 병렬 쓰기 안전
      loop_counters: Annotated[dict[str, int], merge_dicts]  # edge_id → 횟수
  ```
- 각 DSL 노드 → `add_node(node_id, wrapper, retry_policy=...)`. wrapper는 템플릿 렌더링 → `execute()` → `node_runs` 기록 → 이벤트 발행 → `{"outputs": {node_id: out}}` 반환.
- 단일 출력 핸들 노드 → `add_edge`. 분기 노드 → `add_conditional_edges(node_id, router)`; router는 출력과 `loop_counters`를 보고 다음 노드(들)를 반환하며 back-edge를 탈 때 카운터를 증가시킨다.
- `merge` → `add_edge([src1, src2, ...], merge_id)` (모든 선행 노드 완료 대기).
- `recursion_limit` = `노드 수 × (1 + Σ maxIterations)` 로 자동 산정.
- 컴파일 결과는 워커 프로세스 내에서 `dsl_hash` 키로 LRU 캐시.

### 5.3 실행 흐름
1. `POST /workflows/{id}/runs` → 버전 확정 → 검증 → `runs(status=queued)` → 큐 등록.
2. 워커: `status=running`, `graph.astream(input, config={"configurable": {"thread_id": run_id}}, stream_mode=["updates","custom"])`.
3. 이벤트(`node_started`, `node_token`, `node_finished`, `node_failed`, `run_waiting`, `run_finished`, `run_failed`)를 Redis 채널 `run:{id}`에 발행.
4. `GET /runs/{id}/events`(SSE)가 먼저 `node_runs`에서 지난 이벤트를 재구성해 보낸 뒤 Redis 구독으로 이어서 중계한다(새로고침 후에도 상태 복원).

### 5.4 Human-in-the-loop
- `human_approval` 노드 안에서 `interrupt({message, review})` → 체크포인트 저장, `runs.status=waiting`, 워커는 해당 실행을 종료.
- `POST /runs/{id}/resume {nodeId, decision, comment?, editedValue?}` → 상태가 `waiting`이고 대기 노드가 일치할 때만 허용 → 큐 재등록 → 워커가 `Command(resume=payload)`로 이어서 실행.

### 5.5 LLM 게이트웨이 (소형 모델 대응)
- 인터페이스: `chat(model, messages, *, schema=None, temperature, timeout) -> {text | data, tokens_in, tokens_out}`; 구현은 Ollama(OpenAI 호환 및 네이티브 `format` 사용).
- `schema`가 있으면 Ollama `format`에 JSON Schema를 전달하고 Pydantic으로 검증. 실패 시 검증 오류를 메시지에 덧붙여 최대 2회 재요청, 그래도 실패하면 `StructuredOutputError`.
- 모델별 동시 호출 수를 Redis 세마포어로 제한(기본값 = 배포 설정 `OLLAMA_NUM_PARALLEL`과 동일).
- `classifier`는 항상 `schema` 모드로 호출해 카테고리 ID 외의 값이 나오지 않게 한다.

---

## 6. API 요약
| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/node-types` | 노드 스펙 목록(JSON Schema 포함) |
| GET/POST | `/workflows` | 목록 / 생성 |
| GET/PUT/DELETE | `/workflows/{id}` | 조회 / draft 저장 / 삭제 |
| POST | `/workflows/{id}/validate` | draft 검증 결과 |
| POST | `/workflows/{id}/runs` | 실행 요청 `{inputs}` → `{runId}` |
| GET | `/workflows/{id}/runs` | 실행 이력 |
| GET | `/runs/{id}` | 실행 상태·결과 |
| GET | `/runs/{id}/nodes` | 노드별 실행 기록 |
| GET | `/runs/{id}/events` | SSE 실시간 이벤트 |
| POST | `/runs/{id}/resume` | 승인 대기 재개 |
| POST | `/runs/{id}/retry` | 실패 지점부터 재실행 |
| POST | `/runs/{id}/cancel` | 취소 |

---

## 7. 에디터 (web)
- **캔버스**: @xyflow/react, 좌측 팔레트(`/node-types`의 category별), 드래그로 노드 추가, 핸들 간 연결, ELKjs "자동 정렬" 버튼, 되돌리기/다시하기.
- **설정 패널**: 선택한 노드의 `configSchema`로 RJSF 폼 렌더링. 템플릿 입력란은 `{{`를 치면 사용 가능한 변수(선행 노드의 출력 필드) 자동완성.
- **검증 표시**: 저장 시 `/validate` 결과를 노드·엣지에 빨간 배지로 표시.
- **테스트 실행**: Start 입력 폼 → 실행 → SSE로 노드 상태(대기/실행/성공/실패/승인대기) 색상 표시, LLM 토큰 스트리밍 미리보기.
- **실행 기록 패널**: 노드 클릭 시 해당 `node_runs`의 입력·출력·오류·소요 시간·토큰.
- **승인 UI**: `waiting` 실행은 승인/반려/수정 입력 다이얼로그 표시.
- 상태 관리: Zustand(편집 그래프, 선택, 실행 상태). draft는 변경 후 1초 디바운스로 자동 저장.

---

## 8. 오류 처리
| 상황 | 처리 |
|---|---|
| 노드 일시 오류(네트워크, Ollama 과부하) | 노드 `retry`(기본 2회, 지수 백오프) — LangGraph `RetryPolicy` |
| 노드 타임아웃 | 노드 `timeoutSec`(기본 120) 초과 시 실패로 간주, 재시도 정책 적용 |
| 재시도 후에도 실패 | 노드 `onError`: `fail`(기본, 실행 실패) 또는 `default`(설정된 기본 출력으로 계속) |
| 구조화 출력 실패 | 5.5의 재요청 후 노드 오류로 처리 |
| 검증 실패 | 실행 요청 거부(422), 오류 목록 반환 |
| 워커 크래시 | 큐 재전달 → 같은 `thread_id`로 체크포인트부터 재개 |
| 실패한 실행 재시도 | `/retry` → 체크포인트에서 재개, 성공 노드는 재실행하지 않음 |
| 루프 한도 초과 | 4.4 규칙 4에 따라 탈출 경로로 진행(오류 아님) |

---

## 9. 관측성
- `node_runs`에 노드별 입력(렌더링된 프롬프트 포함)·출력·오류·소요 시간·토큰 기록.
- `runs` 목록 화면: 상태, 소요 시간, 총 토큰.
- 사용량 지표는 비용 대신 토큰 수와 모델 호출 시간(과금은 하위 프로젝트 6).
- 서비스 로그는 구조화 JSON(`run_id`, `node_id` 포함).

---

## 10. 테스트 전략
| 레벨 | 대상 | 방법 |
|---|---|---|
| 단위 | 검증기 | 규칙별 정상/위반 DSL 픽스처 → 기대 오류 코드 |
| 단위 | 컴파일러 | 패턴별 golden DSL(Chaining, Routing, Parallel, Evaluator 루프, HITL) + `FakeListChatModel` → 방문 순서·최종 출력 검증 |
| 단위 | 노드 | 노드별 `execute()`를 가짜 LLM/HTTP로 테스트 |
| 단위 | LLM 게이트웨이 | 구조화 출력 재요청, 세마포어, 타임아웃 |
| 통합 | api + worker + Postgres + Redis | docker compose, 가짜 LLM: 실행→대기→워커 재시작→재개 시나리오 |
| 통합(선택) | 실제 Ollama | 초소형 모델(0.5–1B)로 형식만 검증 |
| E2E | web | Playwright: 노드 배치→연결→설정→실행→결과 확인, 승인 흐름 |

---

## 11. 이후 단계로 넘길 항목
- AI 코파일럿(자연어 → DSL 생성 → 검증기 → 캔버스): DSL JSON Schema와 검증기 오류 메시지를 그대로 LLM 피드백으로 사용.
- 에이전트 노드(도구 호출), Orchestrator-Workers(LangGraph `Send`): 중대형 모델 도입 시.
- 오류 전용 분기 핸들, 코드 실행 노드(샌드박스), RAG, 트리거, 멀티테넌시·인증, 과금.
- 규모 확대 시 실행 엔진을 Temporal로 교체할 수 있도록 `worker`의 실행 인터페이스를 좁게 유지.
