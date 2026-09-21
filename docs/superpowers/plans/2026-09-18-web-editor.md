# 웹 에디터(Plan 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the engine a browser. A person who is not a developer places nodes, fills in settings with autocomplete over the variables that are actually available, sees validation errors where they happened, runs the workflow, watches it run, approves what it asks, and reads back what each node did.

**Architecture:** A Next.js App Router app whose **server** holds `ENGINE_API_TOKEN` and proxies every engine call at `/api/engine/*`. The browser never sees the token and never talks to the engine directly — it cannot, because `EventSource` has no way to send an `Authorization` header and the engine has no CORS middleware. The editor's source of truth is a plain DSL document; React Flow renders a view derived from it, and every edit goes through a pure command so undo/redo and autosave are one mechanism, not three.

**Tech Stack:** Node 22, pnpm, Next.js 16 (App Router), React 19, TypeScript strict, Zustand, `@xyflow/react`, ELKjs, `@rjsf/core` + `@rjsf/validator-ajv8`, CodeMirror 6, vitest + `@testing-library/react`, Playwright.

**Design:** `docs/superpowers/specs/2026-09-18-web-editor-design.md` (이하 **3 설계**)
**선행:** Plan 1 엔진 코어(PR #1), Plan 2a 런타임 코어(PR #2), Plan 2b HTTP·보안·운영(PR #4)

---

## Conventions for every task

Every task in this plan follows the same rules. They are written once here; the tasks do not repeat them.

1. **Two working directories.** Frontend work is `cd /d/AI/agentic-workflows/apps/web` with `pnpm …`. The two engine tasks (3, 4) are `cd /d/AI/agentic-workflows/services/engine` with `uv run …`.
2. **TDD.** Write the failing test first, run it and see it fail *for the stated reason*, then implement. A test that passes before the implementation is a broken test — find out why before continuing.
3. **Every task ends green.** `pnpm test` and `pnpm lint` and `pnpm typecheck` all pass before the commit; an engine task also needs `uv run pytest -q` and `uv run ruff check .`. A task never leaves the suite red for the next one to fix.
4. **Docker-free by default.** Only the E2E suite (`pnpm e2e`) needs the compose stack. `pnpm test` must pass with Docker unreachable.
5. **Bound every wait.** No test may hang. Playwright gets an explicit `timeout` on every `waitFor`; vitest tests use fake timers rather than real sleeps.
6. **No API mocks in E2E.** E2E runs against the real compose stack (3 설계 §11). Unit and component tests may stub `fetch` at the module boundary, never with a fake engine that reimplements the contract.
7. **The token never reaches the browser.** Every module under `lib/engine/` starts with `import "server-only"`. A test asserts the client bundle does not contain the token — see Task 2.
8. **Commit from the repo root** (`/d/AI/agentic-workflows`) with exactly two `-m` arguments, the second being the trailer:

```bash
git commit -m "<subject>" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

9. **Do not push.** The branch is finished as a whole at the end.
10. **Korean user-facing text.** Every string a user reads is Korean, like the engine. Comments, identifiers and test names are English.
11. **Never put a secret, a run payload or a URL query string in a log.** The BFF logs method, path and status — nothing else. The browser console logs nothing about request bodies.
12. **The plan's code is a starting point, not something to copy.** Where it disagrees with what the real library does, the library wins; record the difference in the task's post-review note.
13. **Visual design goes through `.claude/skills/frontend-design/SKILL.md`**, installed in this repo at the user's request. Read it before Tasks 7, 9, 16, 17 and 19 — the tasks that produce something a person looks at. **Scope it deliberately** (see 시각 디자인 below): its aesthetic-direction rules apply everywhere; its scroll-driven-website rules do not apply to this product at all.

---

## 시각 디자인 — 스킬을 어디에 적용하는가

`.claude/skills/frontend-design/SKILL.md`은 두 층으로 읽는다. 둘을 구분하지 않고 적용하면 워크플로 에디터가 못 쓰는 물건이 된다.

**적용한다 — "Design Thinking"과 "Frontend Aesthetics Guidelines"**

- 하나의 미학 방향을 정하고 끝까지 밀어붙인다. 색은 CSS 변수로 통일하고, 지배색 하나에 날카로운 강조색을 쓴다.
- **Inter·Roboto·Arial·시스템 폰트 금지.** 흰 배경 위 보라색 그라데이션 같은 뻔한 조합 금지. 표시용 서체와 본문용 서체를 짝지어 고른다. 한국어가 1급 언어이므로 **한글 글꼴이 실제로 존재하는 조합**이어야 한다 — 라틴 전용 표시 서체를 고르고 한글을 폴백에 맡기면 화면 절반이 다른 서체로 렌더된다. 이건 원본 스킬이 다루지 않는 제약이고, 여기서 이긴다.
- 배경은 단색으로 때우지 않는다. 캔버스 격자, 미묘한 노이즈, 깊이감 있는 그림자처럼 맥락에 맞는 질감을 쓴다.
- 페이지 로드와 패널 전환에 의도된 모션을 쓴다. CSS 우선.

**적용하지 않는다 — "Scroll-Driven Website Design Guidelines"**

이 절 전체(6rem 히어로 제목, 10–15vw 마키 텍스트, 카드·컨테이너 금지, 섹션마다 다른 진입 애니메이션, 핀 고정 스크롤, GSAP 카운트업)는 **스크롤로 읽는 마케팅 사이트를 위한 규칙**이다. Plan 3이 만드는 것은 노드 캔버스와 조밀한 설정 패널을 가진 **도구 UI**다. 이 제품에는 스크롤로 읽는 화면이 하나도 없다.

특히 **"NEVER use cards or containers"는 여기서 뒤집힌다.** 노드 패널, 실행 기록 행, 승인 대화상자는 경계가 분명해야 읽힌다. 스킬의 규칙을 그대로 따르면 검증 배지가 어느 노드 것인지 알 수 없게 된다. 도구 UI에서 밀도와 가독성은 미학보다 우선한다 — 그것 자체가 이 맥락의 미학이다.

나중에 랜딩 페이지나 소개 화면이 생긴다면 그때는 이 절을 그대로 적용한다. 지금은 해당 화면이 없다.

**Task 1에서 정할 것**: 미학 방향 한 줄, 서체 짝(한글 포함), 색 토큰(`--bg`, `--fg`, `--accent`, 상태색 4종: 대기·실행·성공·실패). 이후 모든 Task는 그 토큰만 쓴다. Task 19가 워크플로 목록·시크릿 화면으로 그 방향이 실제로 보이는 첫 화면이다.

---

## File structure

**New — `apps/web/`**

| File | Responsibility |
|---|---|
| `package.json`, `tsconfig.json`, `next.config.ts`, `eslint.config.mjs` | Project skeleton |
| `vitest.config.ts`, `playwright.config.ts` | Test runners |
| `app/layout.tsx`, `app/page.tsx` | Shell, workflow list |
| `app/workflows/[id]/page.tsx` | The editor screen |
| `app/api/engine/[...path]/route.ts` | BFF proxy — the only place the token is used |
| `app/api/health/route.ts` | Liveness, and the target `http_request` calls in E2E |
| `lib/engine/client.ts` | `server-only` fetch wrapper: base URL, token, error envelope |
| `lib/engine/types.ts` | TypeScript types mirroring the engine's responses |
| `lib/engine/paths.ts` | The proxy's path allowlist. Pure, testable |
| `lib/dsl/document.ts` | The DSL document type and its invariants |
| `lib/dsl/commands.ts` | `addNode`, `connect`, `setConfig`, … — pure `(dsl) => dsl` |
| `lib/dsl/ids.ts` | `<type>_<n>` generation |
| `lib/dsl/history.ts` | Undo/redo stack with position merging |
| `lib/dsl/layout.ts` | ELKjs auto-layout |
| `lib/template/parse.ts` | `{{ … }}` ranges, reference extraction |
| `lib/template/complete.ts` | Autocomplete candidates from `variables` + `outputSchema` |
| `lib/errors/korean.ts` | pydantic message → Korean mapping |
| `store/index.ts` + `store/{graph,validation,save,run}.ts` | Zustand slices |
| `components/canvas/*` | React Flow canvas, node and edge renderers, palette |
| `components/panel/*` | Settings / policy / label tabs, schema editor |
| `components/template/*` | CodeMirror template input |
| `components/run/*` | Run form, node status, trace panel, approval dialog |
| `components/dialogs/*` | Conflict dialog, confirmations |
| `e2e/*.spec.ts` | Playwright |
| `Dockerfile` | Runtime image |

**Modified**

| File | Change |
|---|---|
| `services/engine/engine/api/routers/workflows.py` | `/validate` returns `nodes` (Task 3) |
| `services/engine/engine/nodes/http_request.py` | `category = "Action"` (Task 4) |
| `deploy/docker-compose.yml`, `deploy/.env.example` | `web` service, `WEB_PORT`, `ENGINE_API_URL` (Task 20) |
| `services/engine/README.md` | Link to the editor's template help (Task 22) |
| `README.md` (repo root, new) | How to run the whole thing |

---

## Task 0: Spike — does SSE actually stream through a Next.js Route Handler?

**Everything in §8 of the design depends on this and nothing else in the plan proves it.** If the route handler buffers the response, the run screen does not work at all, and we need to know that before writing twenty tasks that assume it.

This is a spike: throwaway code, kept only long enough to answer the question, then deleted. Record the answer in the post-review note.

- [x] Create a scratch Next.js app (or `apps/web` itself, before Task 1's tests exist).
- [x] Add a route handler that returns a `ReadableStream` emitting `data: <n>\n\n` once per second for ten seconds, with `Content-Type: text/event-stream`.
- [x] Add a second route handler that **proxies** the first through `fetch`, passing `response.body` straight to the `Response` constructor.
- [x] Measure arrival times with `curl -N` against both, in `next dev` **and** in `next build && next start`. Production is the one that matters; dev has different buffering.

```ts
// app/api/spike/route.ts
export const runtime = "nodejs"        // the edge runtime's fetch has different streaming semantics
export const dynamic = "force-dynamic" // otherwise the route can be statically optimised away

export async function GET() {
  const encoder = new TextEncoder()
  const stream = new ReadableStream({
    async start(controller) {
      for (let i = 0; i < 10; i++) {
        controller.enqueue(encoder.encode(`data: ${i}\n\n`))
        await new Promise((r) => setTimeout(r, 1000))
      }
      controller.close()
    },
  })
  return new Response(stream, {
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no" },
  })
}
```

- [x] **Answer these four questions in writing:**
  1. Do chunks arrive one per second, or all at once at the end? In dev and in prod?
  2. Does proxying through `fetch` preserve that, or does the proxy buffer?
  3. Is `Last-Event-ID` visible in the proxied request's headers?
  4. When the client disconnects, does the upstream `fetch` get aborted? (If not, a closed browser tab leaves an engine SSE connection — and its Redis pubsub connection — open forever. That is a resource leak the engine cannot defend against.)
- [x] For (4), if `AbortSignal` is not wired automatically, prove that passing `request.signal` into the upstream `fetch` does it.
- [x] Delete the spike code. Write the post-review note with the measured numbers.

**If chunks do not stream:** try `export const runtime = "nodejs"` (should already be set), removing any `compress` middleware, and check whether a reverse proxy in the way is buffering. If Next.js itself buffers in production, the fallback in 3 설계 §12 applies: a separate tiny proxy process. Do not proceed to Task 2 until this is answered.

- [x] Commit the note only (no code).

---

## Task 1: Project skeleton

- [x] `pnpm create next-app apps/web` with TypeScript, ESLint, App Router, no Tailwind decision yet — pick Tailwind, it keeps component CSS out of this plan's way.
- [x] Set `"strict": true`, `"noUncheckedIndexedAccess": true` in `tsconfig.json`. The second one matters: the editor indexes into `nodes[id]` constantly and TypeScript should force the missing case to be handled.
- [x] Add vitest with `environment: "jsdom"` for component tests and `environment: "node"` for `lib/` tests — use vitest projects so both run under one `pnpm test`.
- [x] Add scripts: `dev`, `build`, `start`, `test`, `test:watch`, `lint`, `typecheck`, `e2e`.
- [x] Write one trivial test in `lib/` and one component test, so both projects are proven to run.
- [x] Add `apps/web` to the repo `.gitignore` exceptions as needed (`node_modules`, `.next`).
- [x] **미학 방향과 토큰을 정한다** (시각 디자인 절): 방향 한 줄, 서체 짝(한글 글꼴 포함, Inter·Roboto·Arial 금지), 색 토큰과 상태색 4종을 `app/globals.css`의 `:root`에 쓴다. 이후 Task는 이 토큰만 참조한다.

**Verification**
- [x] `pnpm test` passes with two tests.
- [x] `pnpm typecheck` and `pnpm lint` pass.
- [x] `pnpm build` succeeds.
- [x] Commit: `chore(web): scaffold the Next.js editor app`

---

## Task 2: The BFF proxy

The only place `ENGINE_API_TOKEN` is used. Get this wrong and either the editor cannot talk to the engine or the token leaks.

**Tests first** (`lib/engine/paths.test.ts`, `app/api/engine/route.test.ts`):

- [x] `isAllowed("/workflows")`, `/workflows/<uuid>`, `/workflows/<uuid>/runs`, `/runs/<uuid>/events`, `/node-types`, `/secrets`, `/healthz` → true.
- [x] `isAllowed("/openapi.json")`, `/docs`, `/redoc`, `/` → false. A blocked path gets 404, not 403 — do not confirm what exists.
- [x] Path traversal: `/workflows/../openapi.json` and its percent-encoded forms are rejected. Assert on the **normalised** path, and write a test for `%2e%2e%2f` specifically.
- [x] The proxy adds `Authorization: Bearer <token>` — asserted by inspecting the upstream `fetch` call, not by hitting a real engine.
- [x] An engine 409 with an error envelope comes back **byte-identical** with status 409. Assert on the parsed body: `details.currentRevision` survives.
- [x] `Last-Event-ID: 42` on the incoming request appears on the upstream request.
- [x] `Idempotency-Key` is forwarded.
- [x] Hop-by-hop and dangerous request headers are **not** forwarded: `host`, `connection`, `content-length`, and any client-supplied `authorization` (a caller must not be able to override the token).
- [x] A streaming response's `body` is passed through without being read.
- [x] `request.signal` is passed to the upstream fetch (Task 0's question 4).

**Implementation**

```ts
// app/api/engine/[...path]/route.ts
import "server-only"
import { isAllowed } from "@/lib/engine/paths"

export const runtime = "nodejs"
export const dynamic = "force-dynamic"

const FORWARD_REQUEST = ["content-type", "accept", "last-event-id", "idempotency-key"]
// Never forwarded: authorization (the caller must not override our token), host/connection/content-length
// (hop-by-hop or recomputed by fetch), cookie (the engine has no session concept and it would only leak).

async function proxy(request: Request, path: string[]) {
  const target = "/" + path.join("/")
  if (!isAllowed(target)) return new Response(null, { status: 404 })
  const headers = new Headers()
  for (const name of FORWARD_REQUEST) {
    const value = request.headers.get(name)
    if (value !== null) headers.set(name, value)
  }
  headers.set("authorization", `Bearer ${process.env.ENGINE_API_TOKEN}`)
  const url = new URL(target, process.env.ENGINE_API_URL)
  url.search = new URL(request.url).search
  const upstream = await fetch(url, {
    method: request.method,
    headers,
    body: request.body,
    // @ts-expect-error duplex is required by undici for a streaming body and is not in the DOM types yet
    duplex: "half",
    signal: request.signal,
    redirect: "manual",
  })
  return new Response(upstream.body, { status: upstream.status, headers: responseHeaders(upstream) })
}
```

- [x] `responseHeaders` copies `content-type`, `cache-control`, and drops `content-encoding`/`transfer-encoding` (fetch already decoded the body; re-advertising the encoding corrupts it).
- [x] `isAllowed` works on a **normalised** path. Reject any segment equal to `.` or `..` after decoding, and reject a path that is not one of the seven prefixes.
- [x] Env validation at module load: if `ENGINE_API_URL` or `ENGINE_API_TOKEN` is missing, throw with a message naming the variable. Failing at startup beats 401s nobody can explain.

**The token-leak test** — this is the one that earns its keep:

- [x] After `pnpm build`, grep `.next/static/**/*.js` for the token value used in the build. Assert zero matches. Run it as part of `pnpm test` behind a flag, or as a separate `pnpm test:bundle` invoked by Task 22's final verification.

**Verification**
- [x] All proxy tests pass; the traversal test fails first for the right reason.
- [x] Commit: `feat(web): proxy engine requests server-side with the shared token`

---

## Task 3: `/validate` returns what autocomplete needs (engine)

3 설계 §4.1. Without this the editor either has no autocomplete or reimplements `compute_before` — and a second implementation of the guaranteed-set rule will disagree with the engine on exactly the graphs that are hard.

**Tests first** (`services/engine/tests/test_api_validate.py`):

- [x] A valid chain `start → template_1 → end` returns `nodes` with `template_1.variables == ["start"]` and `end.variables == ["start", "template_1"]`.
- [x] A branch: nodes only on one side of a condition are **not** in the join's `variables` (the ∩ rule), and a merge's are (the ∪ rule).
- [x] `handles` for a `condition` is `["true", "false"]`; for a `classifier` it is its category ids plus `default`; for `human_approval` it is `["approve", "reject"]`.
- [x] `outputSchema` for an `llm` node without `outputSchema` config is the text schema (`{text: string}`).
- [x] A DSL with a structural error returns `issues` **and** `"nodes": {}` — not a 500, and not a partial graph.
- [x] A DSL with only warnings returns both `issues` and a full `nodes`.
- [x] `variables` is sorted, so the response is stable between calls (a set's iteration order is not).
- [x] 201 nodes → `nodes` has 200 entries and `nodesTruncated` is `true`. 200 nodes → no `nodesTruncated` key.
- [x] The existing `/validate` tests still pass unchanged: adding a key must not change `issues`.

**Implementation** (`engine/api/routers/workflows.py`)

```python
MAX_ANALYSIS_NODES = 200  # the editor never renders more; bounds the response like MAX_ISSUES bounds issues


def _node_analysis(analysis: Analysis) -> tuple[dict[str, Any], bool]:
    """Per-node facts the editor cannot compute for itself (3 설계 §4.1).

    Empty when phases 1-2 did not pass: `analyze` stops at the first phase with errors, so `graph` is None
    and there is nothing to report. The editor keeps its previous result in that case rather than losing
    autocomplete the moment a draft is momentarily broken.
    """
    if analysis.graph is None:
        return {}, False
    graph = analysis.graph
    before = compute_before(graph)
    schemas = compute_schemas(graph)
    ids = list(graph.order)[:MAX_ANALYSIS_NODES]
    nodes = {
        node_id: {
            "variables": sorted(before[node_id]),
            "outputSchema": schemas[node_id],
            "handles": list(graph.nodes[node_id].spec.handles(graph.nodes[node_id].config)),
        }
        for node_id in ids
    }
    return nodes, len(graph.order) > MAX_ANALYSIS_NODES
```

- [x] `validate_workflow` calls it and adds `nodes` (and `nodesTruncated` only when true).
- [x] `compute_before`/`compute_schemas` are already imported by `engine.validator`; export them from there rather than reaching into `engine.validator.refs` from the router.
- [x] **Do not** recompute the analysis. `analyze()` already ran; these two functions are cheap re-walks of the graph it returned, but calling `analyze` twice would double the CPU on the editor's hottest endpoint.

**Mutation test**
- [x] Change `sorted(...)` to `list(...)` — a test must fail (stability).
- [x] Change `[:MAX_ANALYSIS_NODES]` to no slice — the truncation test must fail.
- [x] Make `_node_analysis` return `{}` unconditionally — several tests must fail.
- [x] Any survivor is either a missing test or genuinely equivalent; write down which.

**Verification**
- [x] `uv run pytest -q` and `uv run ruff check .` pass.
- [x] Commit: `feat(engine): return per-node variables, schemas and handles from /validate`

---

## Task 4: Normalise the `http_request` category (engine)

- [x] Write a test asserting every registered node's `category` is one of `{"IO", "Logic", "AI", "Human", "Action"}`. It fails on `http_request`'s `"action"`.
- [x] Change `HttpRequestNode.category` to `"Action"`.
- [x] Grep the engine tests for the literal `"action"` and fix any that pinned the old value.
- [x] `uv run pytest -q` and `uv run ruff check .` pass.
- [x] Commit: `fix(engine): give http_request the same category casing as every other node`

---

## Task 5: The DSL document and its commands

The core of the editor. Pure TypeScript, no React, no network — so it can be tested exhaustively and fast.

**Tests first** (`lib/dsl/commands.test.ts`, `lib/dsl/ids.test.ts`):

- [x] `nextId("llm", dsl)` returns `llm_1` on an empty document, `llm_3` when `llm_1` and `llm_2` exist, and **`llm_3` when only `llm_2` exists** (max + 1, not count + 1).
- [x] Deleting `llm_2` then adding an llm gives `llm_3`, never `llm_2` again.
- [x] `start` and `end` get no number and `addNode` refuses a second one.
- [x] Every command returns a **new** object; the input is not mutated (assert with a deep-frozen input).
- [x] `removeNode` removes the node **and every edge touching it**.
- [x] `connect` refuses a duplicate `(source, sourceHandle, target)` triple — the engine reports `EDGE_DUPLICATE` for it, but the editor should not create it in the first place.
- [x] `connect` generates an edge id that does not collide with an existing one.
- [x] `setPolicy(node, {})` removes the `policy` key entirely rather than writing `{}` (3 설계 §5.4 — `dsl_hash` treats them differently).
- [x] `setConfig` replaces the whole config object; it does not deep-merge. A merged update cannot delete a key.

**Implementation**
- [x] `lib/dsl/document.ts`: `EditorDsl = WorkflowDSL & { nodes: (Node & { position: XY })[] }`. Position is editor-only and excluded from `dsl_hash` by the engine, so it rides along in the document.
- [x] `lib/dsl/commands.ts`: one exported function per command, each `(dsl: EditorDsl, …args) => EditorDsl`.
- [x] `lib/dsl/ids.ts`: `nextId` and `nextEdgeId`.

**Verification**
- [x] `pnpm test` passes; the immutability test fails first if a command mutates.
- [x] Commit: `feat(web): model the workflow document with pure edit commands`

---

## Task 6: Undo/redo

**Tests first** (`lib/dsl/history.test.ts`):

- [x] Apply three commands, undo twice, redo once → the document equals the state after the second command.
- [x] A new command after an undo **clears the redo stack**.
- [x] The stack is capped at 50; the 51st push drops the oldest and undo still works 50 times.
- [x] Consecutive `setPosition` on the **same node** merge into one entry; on different nodes they do not.
- [x] `commit()` ends a merge window, so a drag followed by a config change is two entries.
- [x] Undo restores positions exactly (a merged drag undoes to before the drag, not to an intermediate frame).

**Implementation**
- [x] `history.ts` keeps `past: EditorDsl[]`, `future: EditorDsl[]` and a `mergeKey: string | null`. Snapshots, not inverse operations (3 설계 §5.1).
- [x] `push(dsl, mergeKey?)`: when `mergeKey` equals the previous push's key, replace the top of `past` instead of adding to it — **no**, the opposite: keep the *older* snapshot (that is the state to return to) and do not push a new one.

Write that last point as a test before implementing it; getting the direction backwards is the obvious bug and it looks fine until you undo a drag.

**Verification**
- [x] `pnpm test` passes.
- [x] Commit: `feat(web): undo and redo over document snapshots`

---

## Task 7: Palette and canvas

- [x] Server component fetches `/node-types` through the proxy and passes it down.
- [x] 시각 디자인 절과 `.claude/skills/frontend-design/SKILL.md`를 먼저 읽는다. 팔레트·노드 렌더는 Task 1에서 정한 토큰만 쓴다.
- [x] Palette groups by `category` in a fixed order: `IO`, `AI`, `Logic`, `Action`, `Human`. An unknown category goes last under "기타" rather than disappearing.
- [x] Drag from the palette onto the canvas → `addNode` at the drop position.
- [x] React Flow renders nodes from the document: label, type icon, id in small grey text, handles from the node's `handles` (from the validation slice; falls back to `["out"]` before the first analysis).
- [x] Connecting two handles calls `connect`; React Flow's own `onConnect` never mutates its internal state directly.
- [x] Delete key removes the selection through `removeNode`/`removeEdge`.
- [x] Node drag end calls `setPosition` with a merge key of `position:<nodeId>`.

**Component tests**
- [x] The palette renders five groups in order with nine node types total.
- [x] Dropping a `llm` on an empty canvas produces a document with `llm_1`.
- [x] Deleting a node with two edges leaves zero edges.

**Verification**
- [x] `pnpm test`, `pnpm typecheck` pass.
- [x] Commit: `feat(web): canvas with a node palette and edge editing`

---

## Task 8: Auto-layout

- [x] ELKjs (`elkjs/lib/elk.bundled.js`) with `layered` algorithm, left-to-right.
- [x] ~~Run it in a **Web Worker**.~~ → **동적 `import()`. 이유는 Task 8 주석.** ELK on a 200-node graph blocks the main thread long enough to drop the drag the user is in the middle of.
- [x] "자동 정렬" button applies the result as **one** `autoLayout` command, so one undo puts every node back.
- [x] Test: a three-node chain gets strictly increasing x positions and the node count is unchanged.
- [x] Test: layout on an empty document is a no-op that does not push a history entry.

**Verification**
- [x] Commit: `feat(web): auto-layout the graph with ELK`

---

## Task 9: The settings and policy tabs

- [x] Selected node → right panel with tabs 설정 / 실행 정책 / 라벨. The policy tab renders only when `defaultPolicy !== null`.
- [x] RJSF with `validator-ajv8`, `schema` = the node type's `configSchema`, `formData` = the node's config.
- [x] **RJSF's own validation is display-only.** The engine is the authority; do not block editing on an ajv error. Set `liveValidate={false}` and `noHtml5Validate`.
- [x] `uiSchema` table per node type supplies Korean labels and field order. Keep it in ~~`components/panel/uiSchema.ts`~~ → **`lib/panel/uiSchema.ts`(JSX 없음). Task 9 주석.** with a comment saying why it cannot come from the engine.
- [x] Widget registry: `x-template` → the (still stub, Task 10) template widget; `start.inputs` and `llm.outputSchema` → the (still stub, Task 11) schema widget.
- [x] Policy tab writes `policy` only when a field differs from `defaultPolicy`; clearing the last override removes the key (Task 5's test already covers the command).
- [x] `http_request` with method POST or PATCH: `maxAttempts` disabled with the Korean note from 3 설계 §5.4.
- [x] ~~Changes are debounced into `setConfig` at 300ms~~ → **히스토리 머지 키. 이유는 Task 9 주석.** so every keystroke is not a history entry.

**Component tests**
- [x] Selecting a `template` node shows the 설정 tab and no 실행 정책 tab.
- [x] Selecting an `llm` node shows both.
- [x] Switching `http_request` method GET → POST disables the attempts input and shows the note.

**Verification**
- [x] Commit: `feat(web): schema-driven settings and policy panels`

---

## Task 10: The template input

The hardest UI in this plan. 3 설계 §6.

**Tests first** (`lib/template/parse.test.ts`, `lib/template/complete.test.ts`):

- [x] `ranges("a {{ b.c }} d")` returns one range with the right offsets and the inner text `b.c`.
- [x] Unclosed `{{` produces no range (so a chip never swallows the rest of the document while typing).
- [x] `{{` inside a Jinja comment or a `{% %}` block is not a reference.
- [x] `candidates(prefix, variables, schemas)`:
  - after `{{ ` → one candidate per node in `variables`, plus the not-guaranteed ones marked
  - after `{{ llm_1.` → the properties of `llm_1`'s `outputSchema`
  - after `{{ llm_1.text` → no further candidates for a `string`
  - a node id that is not in `variables` still yields candidates, flagged `guaranteed: false`
- [x] The insert text is the node **id**; the display label is the node **label**.

**Implementation**
- [x] CodeMirror 6 in a controlled React wrapper. `EditorView` is created once; document changes are applied through transactions, never by recreating the view (recreating it loses IME composition and the cursor).
- [x] `autocompletion({ override: [source] })` with the source built from ~~the validation slice~~ → **그 슬라이스의 *모양*. 왜 아직 값이 없는지는 Task 10 주석.**
- [x] `Decoration.replace` over each reference range with a chip widget — **except** when the selection intersects the range, so a chip under the cursor opens back into text.
- [x] A `ViewPlugin` recomputes decorations on document and selection change.
- [x] The collapsible Korean help text from 3 설계 §6.2, below the editor.

**IME test — do not skip this**
- [x] A Playwright test (this one does need a browser) types Korean via `page.keyboard.insertText` **and** via a composition sequence, and asserts the document contains the composed text once, not twice, and that no chip decoration was applied mid-composition.
- [x] ~~If CodeMirror + IME + decorations fight, fall back per 3 설계 §12: drop the chips~~ → **싸우지 않았다. 칩 유지.** Record the decision in the post-review note.

**Verification**
- [x] Commit: `feat(web): template editor with variable autocomplete`

---

## Task 11: The schema editor

- [x] Form mode: a table of fields (이름, 타입, 필수, 설명). Types offered: 문자열, 숫자, 정수, 참/거짓, 목록, 객체 — the engine's accepted `type` values, no more.
- [x] Nested object/array: one level at a time, with a breadcrumb.
- [x] JSON mode toggle with a plain textarea, for someone who knows what they are doing.
- [x] Switching form → JSON always works. JSON → form works only if the JSON is inside the supported subset; otherwise the toggle is disabled with a Korean explanation, and the JSON stays authoritative. **Never silently drop a keyword the form cannot represent.**
- [x] Validation comes from `/validate` only (3 설계 §7). Do not reimplement `schema_problems`.

**Tests**
- [x] Adding two fields and marking one required produces `{type: "object", properties: {...}, required: ["a"]}`.
- [x] Loading a schema with `anyOf` disables the form toggle and keeps the JSON intact through a round trip.
- [x] Removing the last field produces `{type: "object", properties: {}}`, not `{}`.

**Verification**
- [x] Commit: `feat(web): a form editor for the engine's JSON Schema subset`

---

## Task 12: Autosave and the conflict dialog

**Tests first**
- [x] A change schedules a save 1s later (fake timers). Three changes in 500ms produce **one** save.
- [x] A change during an in-flight save produces exactly one follow-up save after it returns.
- [x] 200 updates the stored revision.
- [x] 409 opens the dialog and does **not** retry on its own.
- [x] "불러오기" replaces the document with `details.draftDsl` and adopts `details.currentRevision`.
- [x] "덮어쓰기" resends with `details.currentRevision`; a **second** 409 reopens the dialog rather than looping.
- [x] A 500 leaves the document untouched, shows the failure in the status bar, and retries on the next change.
- [x] The document is never replaced without the user choosing it.

**Implementation**
- [x] `store/save.ts` holds `revision`, `status: "saved" | "saving" | "pending" | "conflict" | "error"`, and the debounce timer.
- [x] The status bar shows 저장됨 / 저장 중 / 저장 실패 with the last successful time.

**Verification**
- [x] Commit: `feat(web): debounced autosave with explicit conflict resolution`

---

## Task 13: Validation badges and Korean error messages

- [x] After a successful save, call `/validate`; store `issues` and `nodes` in the validation slice.
- [x] `nodes` empty (structural error) → keep the previous `nodes` for autocomplete, per 3 설계 §4.1. Write a test for exactly this.
- [x] Error badge (red) / warning badge (yellow) on nodes and edges by `nodeId`/`edgeId`.
- [x] `field` puts the message next to the matching input in the panel. `handles.<name>` addresses a handle, so a `HANDLE_NOT_CONNECTED` error highlights that handle on the canvas.
- [x] Any error → 실행 button disabled with a tooltip counting the errors.
- [x] `LIMIT_EXCEEDED` → top banner, not a node badge.
- [x] `lib/errors/korean.ts`: map pydantic ~~error types~~ → **메시지 문구. 타입은 전송되지 않는다 — Task 13 주석.** (`string_too_short`, `greater_than_equal`, `literal_error`, `extra_forbidden`, …) to Korean sentences. Unmapped → show the original text.

**Tests**
- [x] Each mapped pydantic type renders its Korean sentence.
- [x] An unmapped type renders the English original, not an empty string.
- [x] A workflow with one error disables the run button; fixing it re-enables it.

**Verification**
- [x] Commit: `feat(web): show validation issues on the canvas in Korean`

---

## Task 14: Starting a run

- [x] 실행 button → dialog with a form generated from the `start` node's `inputs` schema (~~reuse Task 11's renderer in read-and-fill mode~~ → **RJSF. Task 11은 스키마를 편집하지 값을 채우지 않는다 — Task 14 주석.**).
- [x] Submit → `POST /workflows/{id}/runs` with `{inputs, revision}` and a **fresh UUID** `Idempotency-Key` per submission.
- [x] 202 → put `?run=<runId>` in the URL (3 설계 §8.3) and open the run panel.
- [x] 409 `REVISION_CONFLICT` → the draft moved under us; show the conflict dialog from Task 12 **— 단, 이 409는 상대 초안을 싣지 않아 따로 가져와야 한다(Task 14 주석).**
- [x] 422 `VALIDATION_FAILED` → render `details.issues` as badges; this can happen if validation is stale.

**Tests**
- [x] Two rapid submissions send two different idempotency keys (the engine dedupes a *retried* request, not two deliberate runs).
- [x] A failed submission does not put `?run=` in the URL.

**Verification**
- [x] Commit: `feat(web): start a run from a generated input form`

---

## Task 15: The event stream

- [ ] `EventSource("/api/engine/runs/{id}/events")` opened when `?run=` is present and the run is not terminal.
- [ ] Handlers per event type; `node_started`/`node_finished`/`node_failed`/`node_waiting` drive node colours, `node_token` appends to a token preview.
- [ ] **Close explicitly on a terminal event** (3 설계 §8.1). Test: after `run_succeeded`, no further connection is opened.
- [ ] `node_token` never changes a node's status. Test it.
- [ ] Reconnection is `EventSource`'s job; the editor does not implement one. Test that the component does not create a second `EventSource` while one is open.
- [ ] An `error` event while the run is still active shows 연결 끊김 — 재연결 중 in the status bar and clears it on the next message.

**Verification**
- [ ] Commit: `feat(web): drive node state from the run event stream`

---

## Task 16: The trace panel

- [ ] Clicking a node with run history opens 실행 기록: one row per `(execIndex, attempt)` from `GET /runs/{id}/nodes`.
- [ ] Each row shows status, duration, `tokensIn`/`tokensOut`, and expandable `input`/`output`/`error`.
- [ ] `[REDACTED]` renders as a grey badge with a lock icon (3 설계 §8.5). Test that the literal string never appears as plain text.
- [ ] `truncated: true` shows 값이 잘렸습니다.
- [ ] `hasMore: true` from the API shows a "더 보기" control.
- [ ] A node with no rows shows 아직 실행되지 않았습니다, not an empty panel.

**Verification**
- [ ] Commit: `feat(web): per-attempt trace panel for node runs`

---

## Task 17: Approval and cancellation

- [ ] `waitingFor` present → approval dialog with `message`, `review` (read-only unless `allowEdit`), and 승인 / 반려 buttons plus a comment field.
- [ ] Submit → `POST /runs/{id}/resume` with `{nodeId, execIndex, decision, comment?, editedValue?}`. **`reviewedAt` is not sent** (3 설계 §8.4) — write a test asserting the request body has no such key.
- [ ] 409 `RESUME_TARGET_MISMATCH` → 이미 처리된 승인입니다, and refresh the run.
- [ ] 422 → show the engine's message; it is already Korean.
- [ ] 취소 button → `POST /runs/{id}/cancel`. Response `cancelled` → done; anything else → show 취소 중 until `run_cancelled` arrives.
- [ ] Test: the 취소 중 state clears on the `run_cancelled` event and not on a timer.

**Verification**
- [ ] Commit: `feat(web): approval dialog and run cancellation`

---

## Task 18: Restoring a run after a reload

- [ ] On mount with `?run=`, fetch `GET /runs/{id}` and `GET /runs/{id}/nodes` **before** opening the stream (3 설계 §8.3).
- [ ] Terminal run → render the final state and never open a stream.
- [ ] Active run → paint node state from `nodes`, then open the stream from the beginning; the engine replays stored events and the editor's per-node state is idempotent under replay.
- [ ] Test: replaying `node_started` then `node_finished` twice leaves one finished node, not two rows of state.
- [ ] `?run=` pointing at a deleted or unknown run → 404 → clear the parameter and show 실행을 찾을 수 없습니다.

**Verification**
- [ ] Commit: `feat(web): restore run state across reloads`

---

## Task 19: The workflow list and shell

- [ ] 이 화면이 제품의 미학 방향이 처음 드러나는 곳이다. 시각 디자인 절을 다시 읽고 Task 1의 방향을 여기서 완성한다.
- [ ] `/` lists workflows (`GET /workflows`) with name, revision, updated time; 새 워크플로 creates one and navigates to it.
- [ ] Rename in place (`PUT` with `name`).
- [ ] Delete with a confirmation; 409 `WORKFLOW_HAS_ACTIVE_RUNS` → 실행 중인 워크플로는 삭제할 수 없습니다.
- [ ] A secrets screen: list names (`GET /secrets`), add/replace a value (`PUT /secrets/{name}`), delete. **The value input is write-only** — after saving, show only the name and updated time. The API cannot return a value; make sure the UI never implies it could.
- [ ] Secret name validation mirrors the engine's `^[A-Z][A-Z0-9_]{0,63}$` with a Korean explanation, so a bad name is caught before the request.

**Verification**
- [ ] Commit: `feat(web): workflow list and secret management screens`

---

## Task 20: The web container

- [ ] `apps/web/Dockerfile`: multi-stage, `node:22-bookworm-slim`, `next build` with `output: "standalone"`, non-root uid 10001 — same posture as the engine image.
- [ ] `deploy/docker-compose.yml`: a `web` service with `ENGINE_API_URL=http://api:8000`, `ENGINE_API_TOKEN=${ENGINE_API_TOKEN:?}`, `ports: ["${WEB_PORT:-3000}:3000"]`, `depends_on: {api: {condition: service_healthy}}`.
- [ ] Healthcheck hits `/api/health` (no token needed; it does not touch the engine).
- [ ] `deploy/.env.example` gains `WEB_PORT` with a comment.
- [ ] `HTTP_ALLOWLIST` in `.env.example` gains a commented line showing `http://web:3000` for the E2E workflow.
- [ ] Verify: `docker compose config -q` is silent; `docker compose up -d` brings up five services; the editor loads in a browser and lists workflows.

**Verification**
- [ ] Commit: `feat(deploy): add the web container to the compose stack`

---

## Task 21: End-to-end

Against the real stack (3 설계 §11). `playwright.config.ts` has `webServer` disabled — the stack is started by the operator or CI, not by Playwright, because it needs Postgres and Redis too.

- [ ] `e2e/build-and-run.spec.ts`: place `template` → connect start → template → end → fill the template with `{{ start.name }}` chosen from autocomplete → run with `{"name": "세계"}` → assert the output contains 세계.
- [ ] `e2e/approval.spec.ts`: a workflow with `human_approval` → run → dialog appears → 승인 → run succeeds → the trace shows `decision: "approve"`.
- [ ] `e2e/conflict.spec.ts`: two browser contexts on the same workflow → both edit → the second save shows the conflict dialog → 불러오기 replaces the document.
- [ ] `e2e/reload.spec.ts`: start a run that waits on approval → reload → the dialog is still there and the node colours are restored.
- [ ] `e2e/validation.spec.ts`: an unconnected handle → red badge → run button disabled → connect it → enabled.
- [ ] Every `waitFor` has an explicit timeout. No `waitForTimeout` as a synchronisation device.
- [ ] Each spec creates its own workflow and deletes it at the end, so specs can run in any order.

**Verification**
- [ ] `pnpm e2e` passes against a freshly started stack, twice in a row.
- [ ] Commit: `test(web): end-to-end coverage of the five MVP scenarios`

---

## Task 22: Documentation and final verification

- [ ] `apps/web/README.md`: how to run in dev against a local engine, the two environment variables, why the proxy exists.
- [ ] Repo root `README.md`: what the project is, the three subsystems, `docker compose up`, the first-run steps (create a secret, build a workflow, run it).
- [ ] `services/engine/README.md`: link to the editor's template help section, and note that the engine's template rules are mirrored there.
- [ ] Update `docs/superpowers/plans/2026-09-11-roadmap.md`: Plan 3 status → written/landed, and record what moved past the MVP.
- [ ] Final verification, all of it, recorded with real output:
  - `pnpm test`, `pnpm lint`, `pnpm typecheck`, `pnpm build` in `apps/web`
  - `pnpm test:bundle` (the token-leak grep from Task 2)
  - `uv run pytest -q`, `uv run ruff check .` in `services/engine`
  - `docker compose config -q`
  - `pnpm e2e` against the running stack
  - Every commit carries the trailer: `git log --format='%b' origin/feat/runtime-core..HEAD | grep -c Co-Authored-By`
- [ ] Commit: `docs: document the editor and how to run the whole stack`

---

## Design coverage (Plan 3)

| 3 설계 | Task |
|---|---|
| §3.2 BFF 프록시 | 0, 2 |
| §3.3 토큰과 환경 변수 | 2, 20 |
| §4.1 `/validate` 확장 | 3 |
| §4.2 category 정규화 | 4 |
| §5.1 편집 모델 | 5, 6 |
| §5.2 노드 ID | 5 |
| §5.3 설정 탭 | 9 |
| §5.4 실행 정책 탭 | 9 |
| §6 템플릿 입력 | 10 |
| §7 스키마 에디터 | 11 |
| §8.1 실행 시작과 구독 | 14, 15 |
| §8.2 노드 상태 | 15 |
| §8.3 새로고침 복원 | 18 |
| §8.4 승인과 취소 | 17 |
| §8.5 레닥션된 값 | 16 |
| §9 자동 저장과 충돌 | 12 |
| §10 검증 표시와 한국어화 | 13 |
| §11 테스트 | 모든 Task + 21 |
| MVP 문서 9장 팔레트·캔버스 | 7, 8 |
| MVP 문서 9장 워크플로·시크릿 관리 | 19 |
| MVP 문서 12장 E2E | 21 |

---

## Post-review notes

각 Task를 끝낸 뒤, 계획이 틀렸던 점·mutation 결과·측정값을 여기에 적는다. Plan 2b와 같은 규칙이다: **계획서의 코드는 출발점이지 베낄 것이 아니다.**

### Task 0 — spike: does SSE stream through a Next.js Route Handler?

**Answer: yes, in dev and in production, and the proxy preserves it — but only if `request.signal` is
passed to the upstream `fetch`.** Measured on Next.js **16.3.5** (the current `latest`; the plan had
guessed 15) with Node 22.22.2, two route handlers (`/api/spike` producing ten events one second apart,
`/api/proxy` forwarding it) and `curl -N`.

**Q1 — do chunks arrive one per second, or all at once?** One per second, both modes.

| | arrival gaps |
|---|---|
| dev, direct | 1.000, 1.001, 1.001, 1.001, 1.002, 1.001, 1.002, 1.001, 1.001 s |
| prod, direct | 0.996, 1.002, 1.002, 1.002, 1.002, 1.002, 1.002, 1.002, 1.002 s |
| dev, proxied | 0.994, 1.000, 1.000, 1.000, 1.001, 1.001, 0.999, 1.002, 1.000 s |
| prod, proxied | 0.994, 1.000, 1.001, 1.001, 1.000, 1.000, 1.001, 1.000, 1.001 s |

No `next.config` streaming option was needed. `export const runtime = "nodejs"` and
`export const dynamic = "force-dynamic"` were set from the start and not ablated, so they stay in the
Task 2 code as written; whether they are strictly required is unknown and not worth a cycle to find out.

**A measurement trap, recorded because it cost a run and would fool anyone repeating this:** the first
attempt piped curl through `grep -v '^$'` before timestamping, and every event appeared to arrive within
62 ms — the classic "Next.js buffers SSE" symptom. It was `grep`'s own block buffering on a pipe, not
Next.js. Timestamp the lines in the same shell loop that reads them, or use `grep --line-buffered`. A
spike that "proves" buffering with a buffering tool in the pipeline proves nothing.

**Q2 — does proxying through `fetch` preserve it?** Yes. `new Response(upstream.body, …)` hands the
body through without reading it; the gaps above are unchanged through the proxy.

**Q3 — is `Last-Event-ID` visible upstream?** Yes. `curl -H 'Last-Event-ID: 42'` on the proxy produced
`{"n":0,"lastEventId":"42"}` from the upstream, in dev and prod. It is an ordinary request header; the
proxy has to copy it deliberately (it is not in any default forward set), which is what the Task 2 test
pins.

**Q4 — does a client disconnect abort the upstream? Only with `request.signal`.** This is the one that
changes the code, and the control case is what makes it convincing. `curl` was killed at 3 s of a 10 s
stream; the upstream handler logged whether its `ReadableStream.cancel` ran.

| case | upstream outcome |
|---|---|
| proxy **with** `signal: request.signal` (prod) | `upstream cancelled reason=ResponseAborted` at +2.995 s |
| proxy **without** the signal (prod) | no cancel; ran to completion, `upstream done` at +10.004 s |
| direct, no proxy (prod) | `upstream cancelled reason=ResponseAborted` at +2.991 s |

Dev behaved identically (cancelled at +2.986 s with the signal, never without it).

So the abort chain is browser → Next (which does propagate a disconnect into a route handler's
`ReadableStream.cancel`) → `request.signal` → upstream `fetch` → engine. Drop the signal and the chain
breaks at the proxy.

**Why the control case matters more than it looks.** The spike's stream is finite, so the no-signal case
still ended — after ten seconds. The engine's stream for a live run is not finite: it ends at the
terminal event. A closed tab on a `waiting` run would hold an engine SSE connection, and with it one
Redis pubsub connection and the Postgres connection it borrows while paging, for as long as the run
waits — up to the 30-day waiting limit (MVP 문서 11.1). `engine/events/stream.py`'s own docstring says
nothing in that module caps how many streams can be open at once, so nothing on the engine side would
have caught it. **Task 2's `signal: request.signal` is a resource-leak fix, not a tidiness one, and its
test is not optional.**

**Bonus, outside the four questions but it de-risks Task 2's central snippet.** The same spike proxied a
`POST` with `body: request.body` and `duplex: "half"`:

- It works on Node 22 / undici. The `@ts-expect-error` on `duplex` is still needed — the DOM types have
  not caught up.
- The upstream's **409 passed through as 409** and the JSON body arrived intact.
- A client-supplied `Authorization: Bearer ATTACKER` was **dropped** and the upstream saw
  `Bearer spike-token`: the header allowlist in the Task 2 sketch does what it claims.
- `Idempotency-Key: abc-123` was forwarded.

**Plan changes from this task**

- Tech Stack: Next.js 15 → **16** (16.3.5 is `latest`; React 19 peer range is unchanged, so nothing else
  in the plan moves). 3 설계 §3.1 updated to match.
- No fallback from 3 설계 §12 is needed. The "separate lightweight proxy process" contingency is dead —
  delete it from consideration rather than carrying it as a live risk.

**Cost:** one throwaway app, 447 MB of `node_modules`, deleted. Nothing was committed but this note.

### Task 1 — project skeleton, and the aesthetic direction

**미학 방향: 계기판 (instrument panel).** 관제실·오실로스코프·콕핏 계측기. 1px 음각 괘선, 모눈종이 캔버스,
등폭 수치 판독, "지금 살아 있다"를 뜻하는 단 하나의 호박색 신호. 취향이 아니라 기능으로 고른 방향이다 —
계측기는 조밀한 정보를 한눈에 읽히도록 설계된 물건이고, 그것이 노드 캔버스와 설정 패널의 제약과 같다.
근거 전문은 `apps/web/app/globals.css` 머리말에 있다.

- **서체**: IBM Plex Sans KR (표시·본문) + IBM Plex Mono (판독). Plex는 엔지니어링 서체로 그려진 물건이라
  중립적 UI 산세리프에 없는 성격이 있고, KR 컷이 한글을 덮으므로 제목과 본문이 서로 다른 패밀리로
  갈라지지 않는다. 등폭은 라틴·숫자 전용 문맥(노드 id, exec_index, 소요 시간, 토큰 수)에만 쓰고, 섞이는
  곳에서는 같은 슈퍼패밀리의 형제로 폴백한다.
- **색**: 차가운 흑연 잉크 5단계 + 호박색 신호 하나. 밝은 테마는 따뜻한 종이. 상태 6종은 색과 **형태를
  함께** 갖는다(○ ◍ ● ✕ ❙❙ ◌) — 호박과 진홍은 8px 배지에서 구분되지 않고, 적록색약에서는 더 그렇다.

**계획이 틀렸던 것**

- **Next.js 15 → 16.3.5.** 16이 현재 `latest`다. Task 0에서 이미 고쳤다.
- `create-next-app`은 **대상의 부모 디렉터리가 없으면 "path is not writable"** 로 거절한다. `apps/`가 없어서
  처음 실행이 실패했다. 메시지가 권한을 가리키지만 원인은 권한이 아니다. `mkdir -p apps` 먼저.
- **vitest 5는 `@types/node` ^22 이상을 요구**하는데 create-next-app은 ^20을 박아둔다. 런타임이 Node 22이므로
  ^22로 올리는 것이 맞다.
- `vite-tsconfig-paths`는 **필요 없다.** Vite가 `resolve.tsconfigPaths: true`로 직접 지원하고, projects는
  선언 파일의 plugins를 상속하므로 중복 선언하면 vitest가 경고한다.
- `package.json`에 `"type": "module"`이 없으면 vitest 5가 ESM 설정 파일을 CJS로 읽고 경고한다.

**Korean 글꼴 — 막힐 줄 알았으나 막히지 않았다, 대신 다른 것이 나왔다**

계획은 "한글 글꼴이 실제로 존재하는 조합"을 요구했다. 확인해보니 Next의 `font-data.json`에서
**`korean` subset을 선언한 폰트가 0개**다(`IBM Plex Sans KR`조차 `["latin","latin-ext"]`만 선언한다).
여기서 멈추고 self-host로 갔다면 헛수고였다 — 실제로 빌드해보니 로더가 Google CSS 전체를 받아
**한글 unicode-range(U+AC00–D7A3)를 포함한 woff2를 485개 자가 호스팅한다.** 한글은 그냥 된다.
`subsets` 옵션이 실제 커버리지의 전부가 아니다.

**그 대신 진짜 결함이 그 밑에 있었다.** 빌드된 HTML을 세어보니
**`<link rel="preload" as="font">`가 376개**였다. Google은 한글 폰트를 무게당 ~100개 unicode-range 조각으로
쪼개고, `next/font`는 자가 호스팅하는 파일마다 preload를 하나씩 뱉는다. 브라우저는 unicode-range로 필요한
조각만 알아서 가져가므로 preload는 전부 비용이다. KR 얼굴에 `preload: false`를 주니 **376 → 3**이 되었고
한글 커버리지는 그대로다. 이건 눈으로 볼 수 없는 종류의 결함이라, 계획이 요구하지 않았어도 세어본 것이
맞았다. `app/layout.tsx`에 근거를 주석으로 남겼다.

**대비(contrast) — 테스트로 만든 것이 값을 했다**

토큰을 정하고 WCAG 대비를 재보니 **5쌍이 기준 미달**이었다.

| 토큰 | 전 | 후 |
|---|---|---|
| dark `--fg-faint` | 3.36 | 4.83 (ink-900) / 4.54 (ink-800) |
| light `--fg-faint` | 2.79 **FAIL** | 4.58 |
| light `--st-queued` | 3.00 **FAIL** | 3.56 |
| light `--accent` | 3.76 | 4.51 |
| light `--st-running` | 4.03 | 4.83 |

그리고 **손으로 짠 확인 스크립트가 놓친 것을 테스트가 잡았다**: 스크립트는 `--ink-900`만 봤는데,
패널 표면인 `--ink-800` 위에서 dark `--fg-faint`가 4.29로 미달이었다. 두 표면을 모두 도는 테스트가
아니었으면 `.instrument-label`이 패널 안에서만 읽히지 않는 상태로 21개 Task를 갔을 것이다.
그래서 이 검사는 일회성 스크립트가 아니라 `lib/design/contrast.test.ts`로 남는다: 본문 토큰 4.5:1,
상태 글리프 3:1(WCAG 비텍스트), 강조 채움 위 텍스트 4.5:1.

**Mutation 결과 (5/5 잡힘)**

| 변형 | 결과 |
|---|---|
| 글리프 중복(실패도 `●`) | 잡힘 — 형태 구분 테스트 |
| CSS에 없는 토큰 이름 | 잡힘 |
| 라벨을 영어로 | 잡힘 |
| 컴포넌트에 hex 리터럴 | 잡힘 — 토큰 규율 테스트 |
| 상태 하나 삭제 | 잡힘 — **단 하나의 테스트만** |

마지막 것이 기록할 값이 있다: 페이지 테스트는 `STATUSES`를 돌며 만들어지므로 `STATUSES`가 줄어드는 것을
원리적으로 잡을 수 없다. 리터럴 `IDS` 배열에 못 박은 커버리지 테스트만이 잡는다. 파생된 테스트는 원본이
줄어드는 것을 검사하지 못한다 — 다음 Task들에서도 같은 함정이 반복될 것이다.

**검증**: `pnpm test` 31 passed (3 files), `pnpm typecheck` clean, `pnpm lint` clean, `pnpm build` 성공,
preload 3개. 다크·라이트 두 테마를 Playwright로 실제 렌더해 눈으로 확인했다.

### Task 2 — the BFF proxy

**요약**: 프록시는 동작하고, 실제 엔진을 상대로 확인했다. 배운 것의 대부분은 **토큰 유출 테스트의 대조군이
내 전제를 두 번 틀렸다고 알려준 데서** 나왔다.

**경로 허용 목록은 접두사 검사다.** 첫 세그먼트를 엔진의 공개 접두사 5개로 제한한다. 전체 라우트 표는
`engine/api/routers/*`와 계속 맞춰야 하고 엔진이 라우트를 추가할 때마다 프록시가 먼저 막는다. 실제로 막아야
할 것은 FastAPI가 라우터 **밖에** 등록하는 `/openapi.json`·`/docs`·`/redoc`뿐이고, Plan 2b의
`TokenAuthMiddleware` docstring이 적어둔 대로 앱 레벨 `dependencies`는 그 경로들을 덮지 못한다. 차단은 403이
아니라 **404** — 어떤 라우트가 실재하는지 알려주지 않기 위해서다.

세그먼트는 **원문과 1회 디코드 양쪽**에서 검사한다. 1회로 충분한 이유는 받는 쪽도 1회만 디코드하고 원문을
함께 보기 때문이다: `%252e%252e`는 1회 디코드하면 `%2e%2e`가 되는데 그건 1회 디코드하는 누구에게도 탈출이
아니고, 그 형태로 다시 오면 그때 검사된다. 잘못된 이스케이프(`%zz`)는 디코드가 던지므로 거부한다 — 받는 쪽이
우리와 다르게 해석할 값을 추측으로 넘기지 않는다.

**토큰 유출 테스트 — 대조군이 두 번 걸렸고, 두 번 다 내가 틀렸다**

계획은 "빌드 후 `.next/static`에서 토큰을 grep하고 0건이면 통과"를 요구했다. 그대로 짜고 대조군
("같은 grep이 **서버** 청크에서는 토큰을 찾아야 한다")을 붙였더니 **대조군이 실패했다.** 이유가 중요하다:

> **Next는 서버 쪽 `process.env.X`를 아예 인라인하지 않는다.** 빌드 시점에 치환되는 것은 `NEXT_PUBLIC_*`
> 뿐이고, 그것도 코드가 실제로 읽는 자리에서만 그렇다. 나머지는 런타임 조회로 남는다.

즉 토큰은 **어떤 빌드 산출물에도 나타나지 않는다.** `server-only`를 지우고 클라이언트 컴포넌트에서 토큰을
읽어도 이 grep은 똑같이 통과한다 — 번들에 들어가는 건 *값*이 아니라 그걸 런타임에 읽는 *코드*이기 때문이다.
계획이 시킨 대로만 했으면 아무것도 증명하지 못하는 테스트를 초록색으로 달고 21개 Task를 갔을 것이다.

다시 짠 뒤 **두 번째 대조군도 실패했다**: `server-only`가 클라이언트 임포트를 막는지 보려고 프로브 페이지를
`app/_server_only_probe/`에 두었는데 빌드가 그냥 성공했다. 원인은 보안 문제가 아니라
**`_`로 시작하는 폴더는 App Router의 private folder라 라우팅되지 않는다**는 것 — 프로브가 애초에 빌드되지
않았다. 폴더 이름에서 `_`를 떼자 빌드가 제대로 거부했다.

최종본은 각각 **반드시 발화해야 하는 대조군**을 가진 검사 두 개다.

| 검사 | 대조군 | 결과 |
|---|---|---|
| 클라이언트 청크에 토큰 0건 | `NEXT_PUBLIC_` 센티넬을 읽는 클라이언트 페이지를 만들어, 같은 grep이 그것을 **찾아야** 한다 | 대조군 1개 청크에서 발견 ✓ / 토큰 0건 ✓ |
| `server-only`가 문다 | 같은 페이지를 `lib/engine/env` 임포트로 바꾸면 빌드가 **실패해야** 한다 | 빌드 거부 ✓ |

교훈 한 줄: **비어 있는 검색 결과는, 같은 검색이 무언가를 찾는 것을 보여주기 전까지 증거가 아니다.**

**`server-only` vs vitest.** `server-only`는 RSC 빌드 밖에서 임포트되면 던지므로 vitest에서 서버 모듈을
불러올 수 없다. `vitest.config.ts`에서 빈 스텁으로 alias했다. 보장이 약해지지 않는 이유는 위 표의 두 번째
검사가 진짜 Next 빌드로 그 가드를 직접 시험하기 때문이다.

**기동 시 환경 변수 검증** (`instrumentation.ts`). 측정한 동작:

- 환경 변수 **없이 `next build`**: 성공한다. 이미지는 비밀 없이 빌드된다 — 의도한 대로.
- 환경 변수 **없이 `next start`**: `Failed to prepare server Error: ... ENGINE_API_URL이(가) 설정되지
  않았습니다`를 찍고 모든 요청이 500이 된다. 401이 아니라는 게 요점이다 — 401은 운영자의 토큰이 틀린 것처럼
  읽히고, 컨테이너가 토큰을 애초에 못 받은 것과 구별되지 않는다.
- 주의: Next는 그 실패 **전에** `✓ Ready`를 먼저 찍는다. 로그 맨 윗줄만 보면 정상 기동으로 보인다.
  Task 20의 healthcheck가 500을 받아 컨테이너를 unhealthy로 표시하는 것이 실질적인 방어선이다.

**실제 엔진을 상대로 확인** (떠 있는 compose 스택):

| 호출 | 결과 |
|---|---|
| `/api/engine/workflows`, `/node-types`, `/healthz` (**토큰 없이**) | 200 |
| `/api/engine/openapi.json`, `/docs` | 404 |
| 실행 이벤트 SSE | `id:`·`event:` 온전, `run_queued`→`run_failed`까지 |
| `Last-Event-ID: 5`로 재연결 | **6번부터** 재개 |

SSE 쪽은 이미 끝난 실행을 재생한 것이라 *실시간* 흐름을 증명하지는 않는다 — 그건 Task 0의 1초 간격 스파이크가
증명했다. 여기서 증명한 것은 헤더 전달과 프레이밍이 실제 엔진을 상대로 온전하다는 것이다.

**Mutation 8/8 잡힘**

| 변형 | 잡은 테스트 수 |
|---|---|
| `signal: request.signal` 제거 | 1 |
| 호출자의 `authorization`을 그대로 전달 | 1 |
| 차단 응답 404 → 403 | 1 |
| `redirect: "manual"` 제거 | 1 |
| 본문을 `await upstream.text()`로 버퍼링 | 1 |
| `last-event-id` 전달 제거 | 1 |
| 질의 문자열 버리기 | 1 |
| 경로 탈출 검사 제거 | 10 |

**계획이 틀렸거나 비어 있던 것**

- 토큰 유출 테스트의 전제 (위). 계획서 Task 2의 해당 항목은 이 노트대로 다시 읽어야 한다.
- Next 16에는 라우트 핸들러용 `RouteContext` 전역 타입이 **없다.** 생성되는 것은 `PageProps`와
  `LayoutProps`뿐이라 `{ params: Promise<{ path: string[] }> }`를 직접 쓴다.
- `ENGINE_API_URL`에 경로가 붙으면 `new URL("/workflows", base)`가 그것을 **버린다.** 계획서 스케치는 이걸
  다루지 않았다. `engineEnv()`가 경로·질의·프래그먼트가 붙은 base를 거부한다.
- 토큰 최소 길이 16자를 프록시에서도 검사한다(`engine/config.py::MIN_API_TOKEN_LEN`). 더 짧으면 엔진의
  토큰일 수 없으므로 매 요청이 401이 될 값을 보내게 된다.
- `pnpm test:bundle`이 프로브를 지운 뒤 `.next/types/validator.ts`에 없는 페이지 참조가 남아 다음
  `pnpm typecheck`가 TS2307로 깨졌다. 스크립트가 마지막에 빌드를 한 번 더 돌려 타입을 재생성한다 —
  검사 스크립트가 다음 명령을 깨진 상태로 남기면 안 된다.

**검증**: `pnpm test` 75 passed (6 files), `typecheck`·`lint` clean, `build` 성공, `test:bundle` 통과.

### Task 3 — `/validate` returns what autocomplete needs

`POST /workflows/{id}/validate`가 `issues` 외에 노드별 `variables`·`outputSchema`·`handles`를 돌려준다.
전부 `analyze`가 이미 계산하던 것이고, API로 나올 길만 없었다.

**계획이 도달 불가능한 코드를 시켰다.** 계획(과 3 설계 §4.1)은 "노드 200개까지만 채우고 넘으면
`nodesTruncated: true`"를 요구했다. 그대로 구현하고 테스트를 썼더니 경계 테스트가 통과하지 못했다 —
198+2=200개 체인에서 `nodes`가 `{}`로 왔다. 원인은:

> `engine/validator/structure.py::MAX_NODES = 100`. 워크플로는 **100개 노드**를 넘을 수 없고, 넘으면
> phase 1에서 `LIMIT_EXCEEDED` **오류**가 나서 `analyze`가 거기서 멈춘다 → `graph is None` → `nodes: {}`.

즉 200개 상한은 **어떤 draft로도 도달할 수 없는 분기**였고, 그 분기를 검사하는 테스트도 쓸 수 없었다.
상한과 `nodesTruncated`를 **삭제**했다. 응답은 `MAX_NODES`가 이미 묶고 있고, 그게 진짜 경계다.
테스트도 상상 속 경계 대신 실제 경계를 못 박는다: 98+2=100개는 `nodes` 100개와 `issues: []`,
99+2=101개는 `LIMIT_EXCEEDED` 하나와 `nodes: {}`.

계획서가 시킨 대로 두었다면 테스트가 없는 죽은 분기를 남기고, 3 설계 §4.1은 계속 거짓을 말했을 것이다.
설계 문서도 함께 고쳤다.

**측정하다 찾은, 에디터에 유리한 성질.** `analyze`는 오류가 난 단계에서 멈추지만 **phase 3(참조·타입)
오류는 graph를 지우지 않는다.** 확인:

| draft | issues | nodes |
|---|---|---|
| 구조 오류(알 수 없는 노드 타입) | `DSL_INVALID` | `{}` |
| 참조 오류(`{{ start.nope }}`) | `REF_UNKNOWN_FIELD` | **채워짐** |
| 경고만(타입 미선언 값을 숫자 칸에) | `TYPE_WARNING` | 채워짐 |

참조를 반쯤 타이핑한 상태가 자동완성이 가장 필요한 순간이므로 이건 다행한 성질이다. 테스트로 못 박았다.

**남은 공백 — Task 10이 부딪힐 것.** 비는 것은 phase 1·2 오류일 때인데, **캔버스에 노드를 새로 놓고 아직
연결하지 않은 상태**가 바로 그 경우다(`HANDLE_NOT_CONNECTED`는 오류다). 에디터는 마지막 성공 분석으로
폴백하지만 거기엔 방금 놓은 노드가 없으므로, 그 노드의 템플릿 칸에서는 자동완성이 비어 있게 된다.

고치려면 `analyze`가 phase 2 오류에서도 graph를 넘겨야 하는데, 그건 Plan 1의 계약을 넓히는 일이고
`tests/test_validator_refs.py::test_graph_is_only_set_after_phases_one_and_two_pass`가 그 계약을 못 박고
있다. **Task 3의 범위가 아니므로 넓히지 않았다.** Task 10에서 실제로 불편한지 먼저 확인하고, 불편하면
그때 Plan 1 계약 변경을 따로 다루는 것이 맞다.

**계획이 비어 있던 것**

- 두 번 `analyze`하지 않는다. `compute_before`/`compute_schemas`는 이미 만들어진 graph를 다시 걷는
  값싼 함수다. `/validate`는 에디터의 가장 뜨거운 엔드포인트(디바운스된 키 입력마다 1회)라 두 배는 비싸다.
- `variables`는 정렬한다. `compute_before`는 frozenset을 돌려주고 그 순회 순서는 프로세스 간 보장되지
  않는다. mutation으로 `sorted`를 `list`로 바꾸자 **4개 테스트가 깨졌다** — 실제로 순서가 다르다는 뜻이다.
- `handles`가 왜 `/node-types`로 못 나오는지: classifier의 핸들은 테넌트가 타이핑한 카테고리 id들이라
  **설정에 따라 달라진다.** condition은 `["true","false"]`, human_approval은 `["approve","reject"]`.

**내 테스트 기대값이 네 개 틀렸다** (엔진이 아니라 내 쪽):

- `template` 노드 출력 스키마에 `additionalProperties`는 없다.
- `start` 노드에 `inputs`를 선언하지 않으면 `{{ start.topic }}`이 `REF_UNKNOWN_FIELD`가 된다.
- `routing.json`은 경고를 내지 않는다 — 보장되지 않는 참조에 전부 `| default('')`가 붙어 있다.
  경고만 내는 draft를 따로 만들어야 했다: **타입을 선언하지 않은** 입력을 숫자 칸에 넣으면 `TYPE_WARNING`.
  같은 자리에 **선언된 문자열**을 넣으면 오류(`TYPE_INCOMPATIBLE`)라서 그걸로는 안 된다.
- 위의 `MAX_NODES` 건.

**Mutation 5/5 잡힘**

| 변형 | 깨진 테스트 |
|---|---|
| `sorted` → `list` | 4 |
| 항상 빈 맵 반환 | 8 |
| `handles`를 항상 `["out"]` | 1 |
| `outputSchema`를 `{}` | 1 |
| `graph is None` 가드 제거 | 2 |

**검증**: `uv run pytest -q` → `1372 passed` (1360 → +12), `uv run ruff check .` clean.

### Task 4 — normalise the `http_request` category

`HttpRequestNode.category`가 `"action"`에서 `"Action"`으로. 나머지 8종은 `IO`/`Logic`/`AI`/`Human`이라
이것만 소문자였고, 팔레트가 category로 묶으므로 그대로 두면 아무도 스타일링하지 않은 여섯 번째 그룹이
화면에 생긴다.

테스트는 노드별 단언이 아니라 **집합**으로 못 박았다: `{item["category"] for item in nodeTypes}`가 정확히
`{"IO", "Logic", "AI", "Human", "Action"}`이어야 한다. 그래야 새 노드 타입이 여섯 번째 그룹을 발명하는 대신
이 다섯 중 하나를 고르도록 강제된다.

레포 전체에서 `"action"` 문자열을 확인했다. 나머지 히트는 전부 Redis 제어 채널 메시지의
`{"runId": ..., "action": "cancel"}` 키라 무관하다. 기존 테스트 중 이 값을 박아둔 것은 없었다.

**검증**: `uv run pytest -q` → `1373 passed` (+1), `uv run ruff check .` clean.

**Task 4 중에 일어난 환경 사고 (코드와 무관, 기록용).** 세션이 잠시 유휴 상태였다가 돌아오니
**샌드박스 컨테이너가 재시작**되어 있었다(PID가 세 자리로 돌아가 있었다). dockerd가 죽어 있어서
testcontainers가 붙지 못했고 — 통합 테스트가 setup에서 에러 — 사용자가 계속 띄워두라고 한 배포 스택도
함께 사라져 있었다. `dockerd` 재기동 + `docker compose up -d`로 복구했다. 이미지는 남아 있어 재빌드는
필요 없었다. 긴 유휴 뒤에는 통합 테스트를 돌리기 전에 `docker ps`를 먼저 확인하는 편이 낫다.

### Task 5 — the DSL document and its commands

`lib/dsl/document.ts`(타입), `lib/dsl/ids.ts`(id 생성), `lib/dsl/commands.ts`(편집 커맨드 8종).
전부 순수 TS — React도 네트워크도 없어서 빠르고 촘촘하게 테스트된다.

**계획보다 강하게 만든 것 하나.** 계획은 id 규칙을 `<type>_<n>`, max+1로 못 박으라고 했다. 그렇게 쓰고
"관례를 따르지 않는 id는 무시한다" 테스트를 붙였다가 기대값이 틀렸는데, 고치면서 더 중요한 걸 알았다:

> 형식 파싱은 *보기 좋은* 번호를 고르는 휴리스틱일 뿐이고, 정확성이 달린 진짜 요구사항은
> **"이미 쓰는 id를 절대 돌려주지 않는다"** 이다.

`llm_01`과 `llm_1`은 다른 문자열이라 공존할 수 있고, 가져온 문서나 손으로 고친 문서는 무엇이든 담을 수
있다 — 예컨대 `llm_1`이라는 id를 가진 `template` 노드. 그래서 형식 규칙에서 *추론*하는 대신 충돌하지
않을 때까지 증가시키는 루프로 **직접 보장**한다. 테스트도 그 불변식을 직접 단언한다.

**계획이 다루지 않았지만 캔버스가 곧 부딪힐 것들.** React Flow는 사용자가 무엇이든 끌어다 놓게 해주므로
커맨드가 막아야 한다. 전부 "문서를 그대로 돌려준다"(`before === after`)로 처리해, 호출자가 히스토리
항목을 쌓지 않고 넘어갈 수 있게 했다.

- 자기 자신으로의 연결 — 엔진의 그래프 규칙에 자리가 없다
- 존재하지 않는 노드를 가리키는 연결
- **명시적 `sourceHandle: "out"`과 생략을 같은 연결로 취급** — 엔진이 `"out"`으로 기본값을 채우므로
  둘은 같은 엣지다. 기본값을 비교하지 않으면 에디터가 `EDGE_DUPLICATE`가 될 엣지를 만들어낸다
- 같은 이유로, 기본 핸들은 **기록하지 않는다.** 모든 엣지에 `"out"`을 명시하면 의미가 완전히 같은 문서의
  `dsl_hash`가 달라져서 아무것도 바뀌지 않은 새 워크플로 버전이 생긴다
- `setLabel("")`은 빈 문자열을 저장하는 대신 키를 지운다

**`setPositions`는 여러 노드를 한 번에 옮긴다** (계획의 `setPosition` 단수형과 다름). 다중 선택 드래그가
한 번의 undo로 되돌아가야 하기 때문이다. Task 6의 병합 창과 맞물린다.

**얼린 입력으로 순수성을 강제했다.** 테스트가 입력 문서를 `Object.freeze`로 깊이 얼려서, 제자리에서
고치는 커맨드는 조용히 통과하는 대신 던진다. 이건 undo/redo가 의존하는 성질이다 — 히스토리가 스냅샷을
보관하므로, 공유된 가변 노드 하나면 편집이 과거까지 바꾼다.

**lint 설정 하나 추가.** `const { policy: _previous, ...rest } = node`는 키를 지우는 유일한 비파괴
관용구인데 `@typescript-eslint/no-unused-vars`가 경고한다. `^_` 무시 패턴을 켰다 — 버리려고 만든
바인딩이라는 표시다.

**Mutation 8/8 잡힘**

| 변형 | 깨진 테스트 |
|---|---|
| `removeNode`가 나가는 엣지만 제거 | 1 |
| `connect` 중복 검사 제거 | 2 |
| `connect`가 기본 핸들도 기록 | 1 |
| `setConfig`가 병합 | 1 |
| `setPolicy`가 빈 객체를 기록 | 2 |
| `connect`가 자기 연결 허용 | 1 |
| `nextNodeId`가 count+1 | 2 |
| `setPositions`가 원본을 변경 | 2 |

**검증**: `pnpm test` 103 passed (8 files), `typecheck`·`lint` clean.

### Task 6 — undo/redo

`lib/dsl/history.ts`. 역연산 대신 **문서 스냅샷**을 쌓는다: `past`(각 커맨드 직전 상태, 오래된 것부터),
`present`, `future`(undo로 지나온 것, 가까운 것부터), 그리고 진행 중인 드래그의 `mergeKey`.

역연산을 구현하지 않는 이유는 코드량이 아니라 **틀릴 수 있다는 것**이다 — 틀린 역연산은 사용자가 흔치 않은
것을 되돌릴 때까지 아무도 모른다. DSL은 512KB 상한이고 스택은 50개라 최악이 묶여 있다. 스냅샷이 안전한
것은 오직 Task 5의 커맨드가 전부 순수하기 때문이다. 제자리에서 고치는 커맨드가 하나라도 있으면 스냅샷까지
같이 바뀐다.

**계획이 경고한 병합 방향.** 계획서는 이렇게 적어뒀다:

> `push(dsl, mergeKey?)`: mergeKey가 직전과 같으면 `past`의 top을 교체 — **아니다, 반대다**: 더 *오래된*
> 스냅샷을 유지하고 새로 쌓지 않는다. 이걸 먼저 테스트로 쓰라, 방향을 거꾸로 잡는 것이 뻔한 버그이고
> 드래그를 되돌리기 전까지는 멀쩡해 보인다.

그대로 따랐고, mutation으로 방향을 뒤집어 확인했다: 정확히 그 한 테스트(`undoes a merged drag to before
the drag, not to an intermediate frame`)만 깨진다. 다른 114개는 전부 통과한다 — 경고가 없었다면 이 버그는
리뷰에서도 살아남았을 것이다.

**계획에 없던 규칙 셋.** 전부 드래그 병합이 인접한 조작으로 새는 것을 막는다.

- **`undo`가 병합 창을 닫는다.** 닫지 않으면 undo 직후의 이동이 방금 돌아간 그 단계로 병합되어 둘이
  한꺼번에 되돌아간다.
- **`redo`도 마찬가지.**
- **`apply`는 문서가 그대로면(`next === present`) 아무것도 하지 않는다.** Task 5의 커맨드는 할 일이
  없을 때 입력을 참조 그대로 돌려주므로, 그걸 기록하면 "undo를 눌렀는데 아무 일도 안 일어나는" 단계가
  스택에 쌓인다.

**상한은 `slice(-MAX_HISTORY)`로 apply와 redo 양쪽에 건다.** 51번째가 가장 오래된 것을 떨어뜨리고, undo는
그 뒤로도 50번 동작한다. 테스트는 60번 편집 후 50번 undo해서 남은 10개가 창 밖이라 되돌아가지 않는 것을
확인한다.

**Mutation 7/7 잡힘**

| 변형 | 깨진 테스트 |
|---|---|
| **병합 방향 반대** | **1** (계획이 경고한 바로 그것) |
| no-op 가드 제거 | 1 |
| `apply`가 `future` 유지 | 1 |
| 상한 제거 | 1 |
| `undo`가 `mergeKey` 유지 | 1 |
| `commit`이 아무것도 안 함 | 1 |
| 키가 달라도 병합 | 6 |

**검증**: `pnpm test` 115 passed (9 files), `typecheck`·`lint` clean.

### Task 7 — palette and canvas

`lib/palette.ts`(그룹핑), `lib/dsl/flow.ts`(문서 → React Flow 매핑), `store/graph.ts`(Zustand),
`components/canvas/{Palette,WorkflowNode,Canvas}.tsx`, 그리고 서버 컴포넌트용 `lib/engine/client.ts`.

**테스트 가능한 것을 컴포넌트 밖으로 뺐다.** 계획은 컴포넌트 테스트로 "드롭하면 `llm_1`이 생긴다",
"엣지 두 개짜리 노드를 지우면 엣지가 0이 된다"를 확인하라고 했다. 그 동작은 사실 **스토어**의 것이고,
jsdom에서 React Flow를 드라이브하는 것보다 스토어를 직접 부르는 쪽이 훨씬 촘촘히 검사된다. 그래서
`store/graph.test.ts`가 그 계약을 지고, 컴포넌트 테스트는 팔레트 렌더링만 맡는다. 캔버스 자체는
**실제 브라우저에서 드래그 앤 드롭**으로 확인했다(아래).

**매핑의 비대칭 하나가 핵심이다.** DSL은 `sourceHandle: "out"`을 **생략**한다 — 의미가 같은 두 문서가
같은 `dsl_hash`를 갖게 하려고. React Flow에는 그런 규칙이 없어서, `sourceHandle`이 undefined인 엣지는
id가 `"out"`인 Handle에 붙지 않고 **노드 중앙에서 선이 나간다.** 렌더링 버그처럼 보이지 불일치로는 안
보인다. `toFlowEdges`가 기본값을 명시적으로 채우고, 그 이유를 테스트가 문장으로 들고 있다.

**계획에 없던 스토어 결정 넷.**

- **`addNodeAt`은 예외를 던지지 않고 `lastError`에 담는다.** 드롭 핸들러는 드래그 이벤트로 예외를 흘려보낼
  수 없는데, 사용자는 왜 아무것도 안 나타났는지 알아야 한다(두 번째 `start`가 그 경우).
- **`removeSelected`는 선택 전체를 한 커맨드로 처리한다.** 노드마다 따로 지우면 undo가 "몇 번 눌러야
  하나" 게임이 된다.
- **드래그 병합 키는 움직이는 노드 *집합*이다** (`position:a,b`). 다중 선택 드래그가 한 단계이고, 다른
  노드를 집으면 새 단계가 시작된다.
- **`deleteKeyCode={null}`.** React Flow 자체 삭제는 문서 뒤에서 내부 상태를 바꾼다. Delete 키를 직접
  받아 `removeSelected`로 보낸다.

**내 테스트의 전제가 틀린 것 하나.** "아무것도 선택되지 않았을 때 아무 일도 안 한다" 테스트가 3개 노드를
기대했는데 2개가 나왔다. 원인은 코드가 아니라 전제였다 — **`addNodeAt`이 방금 놓은 노드를 선택**하므로
"선택 없음" 상태가 아니었다. 그건 캔버스로서 올바른 동작이고(놓자마자 설정 패널이 그 노드에 열린다),
그래서 테스트가 선택을 명시적으로 비우도록 고치고 그 동작 자체를 별도 테스트로 못 박았다.

**테스트 설정 결함 하나.** 팔레트 컴포넌트 테스트가 헤딩 10개를 봤다(5개여야 함). Testing Library의
자동 cleanup은 프레임워크 전역이 주입될 때만 등록되는데 이 프로젝트는 `globals: true`가 아니다.
`vitest.setup.ts`에 `afterEach(cleanup)`을 넣었다. **이게 없으면 모든 렌더가 이전 것 위에 쌓이고
`getAllBy*`가 지난 테스트의 DOM까지 조용히 돌려준다** — 지금 잡지 않았으면 이후 모든 컴포넌트 테스트가
거짓 양성/음성을 냈을 것이다.

**실제 브라우저 확인, 그리고 거기서 나온 것.** Playwright로 떠 있는 엔진에 붙여 팔레트에서 노드 4개를
끌어다 놓았다. 드래그 앤 드롭 동작, 한글 라벨, 계기판 토큰 모두 정상. 그런데 스크린샷이 결함을 하나
드러냈다: **`HTTP 요청`이 `동작`이 아니라 `기타` 그룹에 있었다.**

코드 버그가 아니라 **이미지 드리프트**였다. 배포 스택이 07:00에 빌드된 `engine:local`을 돌고 있어서
Task 4의 `"action"` → `"Action"` 수정이 컨테이너에 없었다. 소스는 `"Action"`, 컨테이너는 `"action"`.
부수적으로 `기타` 폴백이 의도대로 동작한다는 것도 확인됐다 — 모르는 category의 노드가 사라지지 않고
배치 가능한 상태로 남았다.

이미지를 다시 빌드하려니 **샌드박스 프록시 CA 때문에 pypi.org에서 `invalid peer certificate:
UnknownIssuer`** 가 났다. Plan 2b Task 16이 겪은 것과 같은 문제다. 임시 `Dockerfile.sandbox`로 CA를
build 스테이지에 주입해 빌드하고, 파일은 삭제했다(**커밋하지 않는다** — 샌드박스 전용 우회다).
재기동 후 `/node-types`가 `Action`을 돌려주고, 다시 찍은 스크린샷에서 `HTTP 요청`이 `동작` 아래에 있다.
팔레트 순서도 `CATEGORY_ORDER` 그대로: 입출력 / 모델 / 흐름 / 동작 / 사람.

**계획서의 "no cards, no boxes"에 대하여.** `WorkflowNode`는 경계가 있는 판이다. 「시각 디자인」 절이
적어둔 대로 그 규칙은 스크롤 마케팅 페이지용이고, 경계 없는 노드는 검증 배지도 실행 상태도 이고 갈 수
없으며 격자 캔버스 위에서는 떠 있는 텍스트로 읽힌다.

**검증**: `pnpm test` 152 passed (12 files), `typecheck`·`lint` clean, `build` 성공, 실제 브라우저에서
드래그 앤 드롭 확인.

### Task 8 — auto-layout

`lib/dsl/layout.ts`(ELK 그래프 매핑 두 개 + 호출), `lib/dsl/viewport.ts`(가시성 판정),
스토어의 `autoLayout`, 그리고 툴바의 자동 정렬 버튼.

**Web Worker를 쓰지 않기로 했다 — 계획의 근거가 두 군데 틀렸다.** 계획은
"ELK가 200노드 그래프에서 메인 스레드를 사용자가 하고 있는 드래그를 떨어뜨릴 만큼 붙잡는다"고 했다.

1. **200노드짜리 그래프는 존재할 수 없다.** `structure.MAX_NODES`는 100이다(Task 3에서 확인).
2. **자동 정렬은 버튼 클릭이다.** 버튼을 누르면서 동시에 드래그하고 있을 수는 없다.

실제로 재봤다(같은 V8, 각 5회 중앙값):

| 그래프 | 중앙값 | 최대 |
|---|---|---|
| 11 노드 | 16.1ms | 20.2ms |
| 51 노드 | 40.8ms | 67.6ms |
| **100 노드** (엔진 상한) | **51.3ms** | 67.4ms |

51ms는 프레임 서너 개, 명시적 사용자 동작에 대한 눈에 띄지 않는 멈칫이지 멈춤이 아니다. 워커를 쓰면
번들러별 워커 진입점, 메시지 프로토콜, 생명주기, 그리고 "워커 로드 실패" 오류 경로 한 무리가 붙는다 —
51ms를 감추려고. 대신 **동적 `import()`** 를 썼다: `elk.bundled`는 ~1.5MB라, 자동 정렬을 누르기 전까지
초기 번들에서 빼는 쪽이 훨씬 큰 실질 이득이다.

**테스트는 다 통과했는데 기능은 못 쓰는 상태였다.** 브라우저로 열어보니 자동 정렬을 누르면
**캔버스가 하얗게 비었다.** 문서는 완벽히 정확했다 — ELK가 flow 원점 근처에 배치하는데 뷰포트는 사용자가
두고 온 자리에 남아 있어서, 그래프 전체가 화면 밖으로 나간 것이다. 문서를 검사하는 어떤 테스트도 이걸
잡을 수 없다.

고치고 나서 **되돌리기에서 같은 증상이 다시 나왔다.** 자동 정렬 뒤에 맞춰진 뷰포트가, undo가 복원한
흩어진 배치를 비추지 못한다. 여기서 "자동 정렬 뒤에 fitView"는 한 경우만 고치고 그 undo를 방치하는
반창고라는 게 분명해졌다.

그래서 규칙으로 만들었다: **문서가 바뀐 뒤 화면에 노드가 하나도 없으면 뷰를 맞춘다.**
"항상 맞춘다"가 아니다 — 작은 undo마다 뷰포트가 홱 움직인다. `anyNodeVisible`은 순수 함수라
pan·zoom·경계·미측정 컨테이너를 전부 테스트로 못 박았다. 자동 정렬 자체는 그와 별개로 항상 재구성한다:
노드 하나가 우연히 화면에 남아 있어도, 사용자가 보자고 한 것은 새 배치 전체다.

**새 커맨드는 필요 없었다.** 계획은 `autoLayout` 커맨드를 말했지만 `setPositions`가 이미 정확히 그것이다
— 모든 노드를 한 번에 옮기는 것. 한 커맨드이므로 undo 한 번에 전체가 돌아온다.

**Mutation 6/6 잡힘** (한 번은 고친 뒤)

| 변형 | 깨진 테스트 |
|---|---|
| `elk.direction` RIGHT → DOWN | 2 |
| 끊어진 엣지 필터 제거 | 1 |
| `positionsFrom`이 미배치 노드도 포함 | 1 |
| 가시성 판정을 overlap → containment | 1 |
| 빈 문서를 "안 보임"으로 | 1 |
| 중복 실행 가드 제거 | **처음엔 0** → 테스트 보강 후 1 |

마지막 것이 기록할 값이 있다. 원래 테스트는 두 번 호출한 뒤 `layingOut === false`만 봤는데, 가드가
있든 없든 끝에는 false다. 구분되는 관찰은 **undo 깊이**다 — 두 번 배치되면 커맨드가 둘이고 사용자는
undo를 두 번 눌러야 한다. 상태 플래그가 아니라 사용자가 겪는 결과를 단언해야 했다.

**검증**: `pnpm test` 172 passed (14 files), `typecheck`·`lint` clean, `build` 성공,
실제 브라우저에서 배치 → 연결 → 자동 정렬 → 되돌리기까지 확인.

---

### Task 9 — 설정·실행 정책 패널

`lib/dsl/policy.ts`(오버라이드 ↔ 유효값 양방향), `lib/panel/uiSchema.ts`(RJSF uiSchema 생성),
`lib/panel/retry.ts`(엔진이 재시도를 막는 지점), `components/panel/{NodePanel,widgets,templates}.tsx`,
그리고 스토어의 `setNodeConfig`·`setNodeLabel`·`setNodePolicy`·`endEdit`.

**300ms 디바운스 대신 머지 키를 썼다.** 계획은 "모든 키 입력이 히스토리 항목이 되지 않도록 300ms
디바운스"를 말했다. 히스토리는 Task 4에서 이미 머지 키를 갖고 있으므로, `config:<노드 id>` 같은 키로
**같은 노드의 같은 필드에 대한 연속 편집을 한 항목으로 합치면** 같은 목적을 달성한다. 디바운스보다 나은
점이 둘이다. 하나, **마지막 키 입력을 잃지 않는다** — 디바운스는 타이머가 도는 중에 다른 노드를 선택하면
마지막 글자를 삼킨다. 둘, **테스트가 타이머와 경주하지 않는다** — fake timer도, `await sleep(300)`도
필요 없다. 되돌리기 한 번이 "이 필드에 한 편집"을 통째로 되돌리는 사용자 경험은 동일하다.

**`uiSchema`는 `components/`가 아니라 `lib/panel/`에 뒀다.** JSX가 하나도 없는 순수 함수이고,
vitest의 node 프로젝트에서 jsdom 없이 도는 쪽이 맞다. 계획이 경로를 지정한 이유(엔진에서 올 수 없는
이유를 주석으로 남길 것)는 그대로 지켰다.

**위젯 선택은 표가 아니라 스키마에서 끌어냈다.** Korean 제목은 손으로 쓸 수밖에 없지만(엔진에 표시
문자열이 없다), "어느 필드가 템플릿인가"는 엔진이 `x-template`으로 이미 말하고 있다. 손으로 적은 목록은
노드 타입이 필드 하나 늘어나는 순간 낡는다. `SCHEMA_FIELDS`만은 **노드 타입으로 키를 잡았다** — 훗날
`inputs`라는 이름의 평범한 문자열 필드를 가진 노드가 스키마 편집기를 받으면 안 된다.

**`ui:order`는 항상 `*`로 끝난다.** 빠뜨리면 RJSF가 순서에 없는 속성에서 **예외를 던진다** — 엔진이
config 필드를 하나 추가하는 것만으로 패널이 통째로 빈 화면이 된다는 뜻이다. 테스트가 노드 타입 전부에
대해 마지막 원소를 단언한다.

**브라우저로 열어보고 결함 셋을 찾았다. 셋 다 단위 테스트가 구조적으로 못 잡는 것이다.**

1. **폼이 스타일 없이 렌더됐고 머리말에 `HttpRequestConfig`가 떠 있었다.** RJSF의 기본 템플릿은 의도적으로
   맨 HTML이다 — 라벨이 컨트롤과 같은 줄에 붙고, 간격이 없고, 체크박스가 벌거벗었다. 계기판 크롬 옆에서는
   "수수한 페이지"가 아니라 **깨진 페이지**로 읽힌다. `FieldTemplate`·`ObjectFieldTemplate`·
   `BaseInputTemplate`과 `Select`·`Checkbox` 위젯을 채웠다. 머리말은 pydantic 클래스 이름으로,
   사용자에게 아무 뜻도 없고 두 줄 위 패널 헤더를 반복한다 — 루트 객체의 title만 죽였다. 중첩 객체는
   유지한다(`headers`에는 제목이 필요하다).
2. **도움말이 `<label>` 안에 있어서 컨트롤의 접근성 이름을 오염시켰다.** 스크린 리더가 필드에 포커스할
   때마다 설명 문장 전체를 읽는다. `getByLabelText("라벨")`이 못 찾은 것이 이게 드러난 경위인데,
   **테스트가 아니라 마크업을 고쳤다** — 도움말을 `<label>` 밖으로 빼고 `aria-describedby`로 연결했다.
3. **`ObjectFieldTemplate`이 항목 추가 버튼을 떨어뜨려서 `http_request`가 헤더를 아예 못 넣었다.**
   자유 형식 맵(엔진의 `dict[str, str]`, 여기서는 `additionalProperties`)은 **키를 추가할 방법이 있어야만**
   쓸 수 있다. 필드가 아무것도 없는 제목으로만 렌더되고 있었다 — 그 노드가 하는 일의 대부분이 헤더인데.
   복구하고, 버튼이 사라지면 깨지는 테스트를 붙였다.

**RJSF 6은 `idSchema`를 `fieldPathId`로 바꿨다.** 루트 판별(`$id === "root"`)은 그대로다.
`node_modules/@rjsf/utils`의 타입을 보고 확인했고, 옛 이름으로 쓴 코드는 조용히 `undefined`를 읽는다.

**계획이 말한 "GET → POST 전환" 테스트를 실제 전환으로 썼다.** 처음엔 POST 노드 하나와 GET 노드 하나를
각각 렌더하는 테스트 둘이었는데, 그건 전환이 아니다 — 메서드는 **설정 탭**에 있고 시도 횟수는
**실행 정책 탭**에 있어서, 패널이 스토어에서 노드를 다시 읽지 않으면 아무것도 다시 렌더되지 않는다.
`Canvas`가 마운트하는 방식 그대로(스토어 구독) 렌더하는 작은 하네스를 붙여서, 활성 → 비활성 전이를 봤다.

**Mutation 16/16 잡힘** (한 번은 고친 뒤)

| 변형 | 결과 |
|---|---|
| `policyOverride`가 기본값과 같은 값도 저장 | killed |
| 빈 오버라이드가 `undefined` 대신 `{}` (정책·재시도 각각) | killed |
| `retry` 오버라이드 통째로 누락 | killed |
| `effectivePolicy` 병합 순서 뒤집기(기본값이 오버라이드를 이김) | killed |
| `retry`를 필드별이 아니라 객체째 병합 | killed |
| 구조 비교 → 동일성 비교 | **처음엔 0** → 테스트 보강 후 killed |
| 재시도 금지가 모든 노드 타입에 적용 | killed |
| PATCH를 멱등으로 취급 | killed |
| 메서드 생략 시 기본값 GET → POST | killed |
| `x-template`/스키마 필드가 위젯으로 안 감 (각각) | killed |
| `SCHEMA_FIELDS`를 노드 타입과 무관하게 취급 | killed |
| `ui:order`에서 `*` 제거 | killed |
| 모르는 속성에 이름을 제목으로 지어냄 | killed |

살아남은 하나가 기록할 값이 있다. 원래 테스트는 `defaultOutput: null`로 "구조 비교"를 단언했는데,
`null === null`이라 동일성 비교로 바꿔도 통과한다. 실제로 구분되는 경우는 **기본값과 구조가 같은 별개
객체**다 — RJSF는 키 입력마다 `formData`를 새로 만들기 때문에 폼의 사본은 결코 같은 참조가 아니다.
동일성 비교였다면 저장할 때마다 기본값과 똑같은 "오버라이드"를 써서 **저장할 때마다 워크플로 버전이
하나씩 늘어난다.** 테스트를 그 경우로 바꾸고, 대조군(진짜 바뀐 중첩 값은 잡혀야 한다)을 붙였다.

**남은 공백 — Task 10·11이 채운다.** 템플릿 위젯과 스키마 위젯은 지금은 정직한 textarea다. 같은 값을
쓰기 때문에 패널은 반쪽이 아니라 쓸 수 있는 상태이고, `{{` 자동완성(Task 10)과 스키마 폼 편집기(Task 11)가
그 자리에 들어간다. 스키마 textarea는 **blur에서만** 파싱한다 — 타이핑 중인 JSON은 유효하지 않은 JSON이라,
글자마다 그걸 보고하면 필드를 못 쓴다.

**검증**: `pnpm test` 218 passed (18 files), `typecheck`·`lint` clean, `build` 성공,
실제 엔진에 붙은 브라우저에서 설정 탭·실행 정책 탭 스크린샷 확인.

---

### Task 10 — 템플릿 입력

`lib/template/parse.ts`(`ranges`·`openReferenceAt`), `lib/template/complete.ts`(`candidates`·`typedAt`),
`lib/template/schema.ts`(엔진 타입 규칙), `lib/template/context.ts`(문서 + `/validate` → 자동완성 재료),
`components/panel/{TemplateEditor,TemplateHelp}.tsx`, 그리고 Playwright 설정과 픽스처.

**칩은 유지했다.** 3 설계 §12의 대비책(IME와 싸우면 칩을 버린다)은 쓰지 않았다. CodeMirror 6은
composition 중에 DOM을 건드리지 않으면 멀쩡하고, "커서가 든 칩은 펼친다"는 규칙이 그것을 이미 보장한다 —
커서가 있는 범위에는 장식을 얹지 않으므로, 조합 중인 글자 위에 `Decoration.replace`가 올라갈 일이 없다.

**계획의 순서가 틀렸다 — Task 10은 없는 슬라이스를 읽는다.** 계획은 자동완성 소스를 "validation
슬라이스"에서 만들라고 했는데, 그 슬라이스는 Task 13에서 생기고 `/validate`는
`POST /workflows/{id}/validate`다 — **DB에 있는 워크플로 id가 필요하다.** 그 id는 Task 12(자동 저장)와
Task 19(워크플로 목록) 전에는 존재하지 않는다. 그래서 *모양*만 만들었다: `templateContext(dsl, types,
analysis, nodeId)`가 `analysis`를 받고, 지금은 `null`을 넘긴다. Task 13이 값을 채우면 나머지는 그대로
동작한다.

`null`은 `[]`가 아니다. 빈 배열은 "엔진이 아무것도 보장하지 않는다"는 뜻이고, 그러면 모든 후보에
회색 "기본값 필요"가 붙는다 — 아무것도 모르면서 경고하는 셈이다. `null`은 "아직 안 물어봤다"이고
아무 표시도 하지 않는다. 테스트가 둘을 구분해서 못 박는다.

**엔진 타입 규칙을 한 번 복제했고, 그게 이 에디터의 유일한 복제다.** `lib/template/schema.ts`는
`engine/dsl/types.py`의 `kinds_of`·`resolve_path`를 옮긴 것이다. 자동완성은 점(`.`)을 찍는 순간
"여기 뒤에 뭐가 올 수 있나"에 답해야 하고, 글자마다 엔진에 왕복하는 것은 답이 아니다. 대신 **각 테스트가
따라가는 엔진 함수 이름을 적어서**, 엔진이 규칙을 바꾸면 여기가 깨지도록 했다.

바로 그 덕을 봤다. `additionalProperties` 처리에서 **내 기대값이 틀렸다** — 엔진은
`properties` 키가 있을 때만 `additionalProperties`를 따라가고, 없으면 "객체, 내용 모름"(`{}`)으로
떨어진다. 추측하지 않고 엔진을 직접 돌려서 확인했고, 구현이 아니라 테스트를 고쳤다.

**IME 테스트를 처음엔 쓸모없게 썼다. 대조군을 돌려서 알았다.** 네 개가 다 통과하길래 확인 삼아
편집기를 두 군데 망가뜨려 봤다.

| 망가뜨린 것 | 결과 |
|---|---|
| composition 중 문서 되쓰기 가드 제거 | **4 passed** |
| 커서가 든 칩을 펼치는 규칙 제거 | **4 passed** |

둘 다 통과했다 — 그 테스트들은 아무것도 검사하고 있지 않았다. 이유가 각각 달랐다.

1. **가드 쪽**: 픽스처의 부모가 `onChange`를 동기로 반영하니 `value`와 문서가 항상 같고, 되쓰기 분기에
   애초에 닿지 않는다. 그래서 픽스처에 `?lag=1`을 넣었다 — 한 프레임 늦게 커밋하는 부모, 즉 배치·디바운스·
   스토어 왕복을 하는 모든 부모의 모양이다. 그 조건에서만 가드가 의미를 갖는다. **테스트할 수 없는 방어
   코드는 죽은 코드와 구분되지 않는다.**
2. **칩 쪽**: 커서가 늘 참조 바깥에 있어서, 규칙이 있든 없든 칩이 그려졌다. 칩을 만든 뒤 **칩을 클릭해서
   원문으로 펼쳐지는지** 보는 테스트를 따로 넣었다.

그리고 조합 자체도 손으로 만든 `CompositionEvent` + `textContent` 대입으로 흉내내고 있었는데, 그건
CodeMirror의 선택 영역을 통째로 부수는 짓이라 결과가 `녕안`으로 뒤집혀 나왔다 — 에디터가 아니라 하네스가
틀린 것이다. **CDP의 `Input.imeSetComposition`** 으로 바꿨다. 브라우저 자신의 IME 경로다.
대조군을 다시 돌려서 이번엔 둘 다 **실패**하는 것을 확인했다.

**Playwright는 앱이 아니라 픽스처를 띄운다.** `e2e/fixture`를 Vite로 서빙하고 편집기 하나만 마운트한다.
앱을 띄우면 Next·엔진·DB가 모두 필요하고, 그러면 실패했을 때 그게 에디터 탓인지 스택이 안 뜬 탓인지
알 수 없다. 미리 깔린 크로미움은 `PLAYWRIGHT_CHROMIUM_PATH`로 가리킨다 — 환경마다 경로가 다르므로
설정에 박지 않았다.

**한글로 노드를 찾는 것이 안 됐다.** 토큰 앵커가 `[A-Za-z_]`라 첫 한글 자모에서 매칭이 끊기고 팝업이
닫혔다. 좁혀야 할 키 입력이 팝업을 닫는 것이다. `\p{L}`로 넓히고(`u` 플래그), CodeMirror의 기본 매처도
한글을 단어 문자로 보지 않으므로 `filter: false`로 끄고 `candidates`가 거르게 했다. 고른 뒤 문서에
들어가는 것은 여전히 **노드 id**다 — `typedAt`이 대체할 길이를 돌려주므로 `{{ 번llm_2 }}`가 되지 않는다.

**브라우저로 열어보고 결함 넷을 더 찾았다. 세 번째 태스크 연속으로, 단위 테스트가 구조적으로 못 잡는
것들이다.**

1. **모든 설정 폼 아래에 죽은 `+ 항목 추가` 버튼이 있었다 — Task 9에서 이미 커밋한 버그다.**
   pydantic은 모든 모델에 `additionalProperties: false`를 붙이는데, Task 9의 검사는
   `!== undefined`였다. `false`는 자유 형식 맵의 **반대**다. `true`이거나 객체일 때만으로 고쳤다.
2. **스키마 편집기가 아예 렌더되지 않았다.** `llm.outputSchema`는 엔진에서 `anyOf: [{object},{null}]`인데,
   RJSF는 `anyOf`를 **위젯을 보기 전에** 자기 분기 선택기로 렌더한다. 그래서 `ui:widget`이 닿지 않았고,
   패널에는 분기 번호가 든 숫자 입력(`1`)이 떠 있었다. 위젯이 아니라 **필드**(`ui:field`)로 옮겼다.
3. **그래도 분기 선택기가 남았다** — "출력 형식 option 2" 드롭다운이 편집기 아래에 따라붙었다.
   `ui:field`는 값을 편집하는 방식만 갈아끼우지, 선택기를 없애지 않는다. 폼에 주기 전에
   **스키마에서 접었다**(`collapseOptionalSchemas`). 고를 것이 없는 선택이다 — 분기는 "객체"와 "없음"이고,
   빈 편집기가 이미 "없음"이다. 접는 것은 **선언된 스키마 필드의 `anyOf: [<object>, {null}]` 한 모양뿐**이다.
   에디터가 표현 못 하는 분기를 버리면 테넌트가 저장할 수 있는 것이 조용히 좁아진다.
4. **템플릿 규칙이 템플릿 필드마다 하나씩 붙었다.** `llm`은 템플릿 필드가 둘(system·prompt)이라 320px
   패널에 일곱 줄짜리 설명이 두 벌 들어갔다. 위젯에서 빼서 폼 바닥에 한 번만 둔다.

**Mutation 20/21 잡힘**

| 변형 | 결과 |
|---|---|
| 닫히지 않은 블록이 스캔을 멈추지 않음 | killed |
| 주석·구문 블록이 불투명하지 않음 (각각) | killed |
| 문자열 안의 `}}`가 참조를 끝냄 | killed |
| 여는 대시(`{{-`) 유지 | killed |
| `openReferenceAt`이 이미 닫힌 참조를 무시 | killed |
| 필터 뒤에서도 참조를 제안 | killed |
| `end` 노드를 제안 | killed |
| id 대신 라벨을 삽입 | killed |
| 보장 미상(`null`)을 `false`로 보고 | killed |
| 스칼라에서 끝난 참조에 계속 제안 | killed |
| 라벨로 거르지 않음 | killed |
| 선택 프로퍼티 표시 제거 | killed |
| `integer`가 `number`로 안 감 | killed |
| `additionalProperties: true`가 미상이 아님 | **처음엔 survived** → 테스트 추가 후 killed |
| properties 없는 객체가 "없음"으로 떨어짐 | killed |
| 배열 인덱스가 `items`로 안 들어감 | killed |
| 모양으로 객체 추론 제거 | killed |
| 보장 미상이 빈 배열이 됨 | killed |
| 노드 라벨 무시 | killed |
| **스캔 재개 위치를 `at + close.length` → `at`** | **survived (등가 변형)** |

마지막은 등가 변형이다. 닫는 구분자(`}}`·`%}`·`#}`)의 문자들은 어느 여는 구분자도 아니므로, 스캐너가
그 안에서 재개해도 한 글자씩 넘기며 같은 상태로 수렴한다. 구분하는 입력을 만들 수 없어서 둘 다 그대로
뒀다 — 테스트를 위한 테스트를 더하지 않았다.

(하네스 메모: 내 mutation 스크립트가 `|`를 필드 구분자로 썼는데 sed 식 안의 `||`와 부딪혀 넷이 조용히
NO-OP으로 빠졌다. 구분자를 바꿔 따로 돌렸다. **"NO-OP"을 출력하게 해둔 덕에 통과로 착각하지 않았다.**)

**남은 공백.**
- **시크릿 자동완성이 없다.** `{{ secret.NAME }}`은 http_request의 url·headers·body에서만 합법이고
  (`refs.py::_check_ref`), 이름은 `/secrets`에서 온다. 필드 문맥과 새 엔드포인트가 둘 다 필요해서
  Task 10 범위가 아니다. 사람이 직접 칠 수는 있고 엔진이 검증한다.
- **타입 불일치 인라인 표시**(MVP 9장)는 Task 13의 배지가 한다. 여기서는 후보 목록의 회색 글씨로
  형식을 보여주는 데까지만 했다.
- **스키마 필드는 아직 textarea다.** Task 11이 폼 편집기로 바꾼다.

**검증**: `pnpm test` 293 passed (22 files), `e2e` 5 passed (chromium), `typecheck`·`lint` clean,
`build` 성공, `test:bundle` 통과, 실제 엔진에 붙은 브라우저에서 자동완성 → 칩 → 규칙 펼치기까지 확인.

---

### Task 11 — 스키마 에디터

`lib/schema/model.ts`(스키마 ↔ 폼 모델 양방향), `lib/schema/edit.ts`(경로 기반 편집 연산),
`components/panel/SchemaEditor.tsx`, 그리고 `JsonSchemaField`가 textarea 대신 이것을 렌더한다.

**모델은 파생값이 아니라 상태다. 이게 이 태스크에서 제일 중요한 결정이다.** 처음에는 `value` prop에서
`useMemo(() => toModel(value))`로 뽑았다. 테스트 하나가 실패해서 알았다 — **폼 모델은 JSON Schema가
담을 수 없는 것을 담는다.** 이름이 같은 필드 둘, 아직 비어 있는 이름, 타이핑 도중의 이름. 매 키 입력마다
`toSchema`로 왕복하면 그것들이 파괴된다. JSON 객체는 같은 키 둘 중 **마지막 하나만** 남기므로,
`b`를 `a`로 고쳐 치는 순간 필드가 **조용히 사라지고**, 계속 쳐서 `ab`가 되어도 돌아오지 않는다.

그래서 모델을 `useState`에 두고, 함께 저장한 `mirror`(그 모델이 만들어낸 스키마)와 들어온 `value`를
비교한다. 부모가 우리 출력을 되돌려준 것은 외부 변경이 아니고, 되돌리기나 다른 노드 선택은 외부 변경이다.
이 구분을 못 하면 둘 중 하나는 반드시 깨진다.

**내가 쓴 왕복 테스트가 내 구현의 무단 삭제를 잡았다.** 루트의 `description`을 읽기는 하는데 쓰지는
않고 있었다 — 이 모듈이 막으려는 바로 그것이다. 필드의 설명은 자기 행에 살지만 루트의 설명은 행이 없다.
**거부하지 않고 표현했다**: 표 위에 설명 칸을 하나 뒀다.

**표현할 수 없으면 고쳐 쓰지 않고 거부한다.** 엔진이 받는 부분집합(`engine/jsondata.py::schema_problems`)은
폼이 그릴 수 있는 것보다 넓다. `anyOf`·`oneOf`, `enum`, `const`, 수치·길이 범위, `additionalProperties`,
`title`, 타입 배열, `items: true`, `items` 없는 배열, 없는 프로퍼티를 가리키는 `required` — 전부 합법이고
전부 폼에 칸이 없다. 이런 스키마는 **폼 토글이 비활성화되고, 이유를 적고, JSON이 권위를 갖는다.**
모르는 키워드를 조용히 떨어뜨리는 폼 편집기는 폼 편집기가 없는 것보다 나쁘다 — 손실이 실행 전까지
보이지 않기 때문이다.

예외는 **하나**뿐이고 그것도 의미가 같다: `required: []`는 없는 것과 정확히 같은 값을 받아들이므로
생략한다. 테스트가 그 하나를 명시적으로 못 박고, 위 목록 전부는 `toModel(...) === null`을 단언한다.

**`null` 타입은 일부러 안 넣었다.** 엔진의 `_TYPES`에는 있지만, "null만 될 수 있는 필드"는 사람이
선언하려는 것이 아니다. 계획이 적은 여섯 가지가 형식 칸의 전부이고, `null`을 쓴 스키마는 JSON 모드로 간다.

**패널 안에 `+ 항목 추가` 버튼이 둘이었다.** 하나는 자유 형식 맵에 키를 더하는 것(Task 9), 하나는
데이터 형식에 필드를 더하는 것. 같은 글자, 다른 뜻, 같은 화면. 테스트가 둘 중 어느 것인지 가릴 수 없었고
**사람도 가릴 수 없다.** 스키마 쪽을 `+ 필드 추가`로 바꿨다.

**`<label htmlFor>`이 `<div>`를 가리키고 있었다.** 스키마 에디터는 입력 하나가 아니라 입력들의 묶음이다.
`<fieldset>`과 `<legend>`로 바꾸고, 테스트도 `getByRole("group", { name: ... })`로 바꿨다.

**Mutation 변형 하나가 실행 전체를 멈춰 세웠다.** `freeName`의 이름 탐색이 `for (let n = 1; ; n += 1)`
이었는데, 변형이 후보를 고정값으로 바꾸자 **끝나지 않는 동기 루프**가 됐다. vitest의 테스트 타임아웃은
이것을 끊지 못한다 — 이벤트 루프를 양보하지 않으니 타이머가 돌 기회가 없다. 20분을 기다리다 알았다.
두 가지를 고쳤다.

1. **제품 코드의 탐색에 경계를 줬다.** 필드가 `n`개면 `field1..field(n+1)` 중 하나는 반드시 비어 있다
   (비둘기집). 그 경계까지만 돈다. 사용자 지시가 "모든 대기에 경계를 둔다"이고, **끝을 증명할 수 없는
   루프는 대기와 같다.**
2. **하네스의 변형마다 `timeout 120`을 걸었다.** 변형은 코드를 망가뜨리려고 만드는 것이므로,
   망가진 코드가 멈추지 않는 경우를 하네스가 감당해야 한다.

**Mutation이 죽은 코드도 찾아냈다.** `updateFields`의 "해석되지 않는 경로는 무시" 가드를 지워도 아무
테스트가 깨지지 않았다. 살펴보니 두 분기가 모두 도달 불가능이다 — 빈 경로는 `updateAt`이 따로 처리하고,
범위 밖 인덱스는 `fields.map`이 이미 아무 행과도 맞지 않는다. **테스트를 더하지 않고 가드를 지웠다.**

**Mutation 20/20 잡힘** (셋은 테스트 추가 후, 하나는 코드 삭제로)

| 변형 | 결과 |
|---|---|
| 빈 `required` 배열을 기록 | killed |
| 루트가 `type`을 잃음 | killed |
| 빈 스키마가 폼을 못 염 | killed |
| 루트 타입을 아무거나 허용 | killed |
| 객체·스칼라 키워드를 조용히 버림 (각각) | killed |
| **배열 키워드를 조용히 버림** | 처음엔 survived → 테스트 추가 후 killed |
| 없는 프로퍼티를 가리키는 `required` 허용 | killed |
| **객체가 아닌 `items` 허용** | 처음엔 survived → 테스트 추가 후 killed |
| 필드·루트 설명 누락 (각각) | killed |
| 목록이 경로에 투명하지 않음 | killed |
| 객체↔목록 전환이 하위 필드를 버림 | killed |
| 잃을 것이 없는데 경고 / 빈 객체에 경고 | killed |
| **낡은 경로가 무시되지 않음** | survived → **죽은 코드여서 삭제** |
| 목록이 아닌 필드에 항목 타입 설정 | killed |
| 새 필드 이름이 충돌 | killed |
| 중복 이름 미검출 | killed |
| **`trail`이 잎을 지나쳐 계속 걸음** | 처음엔 survived → 테스트 추가 후 killed |

**남은 공백.**
- `enum`·범위 같은 **표현 가능한데 폼에 없는** 것들이 있다. 지금은 JSON으로 보낸다. 넣는다면 형식 칸이
  아니라 행마다 "제약" 서랍을 다는 쪽이 맞아 보이는데, 계획에 없으므로 넓히지 않았다.
- `/validate` 메시지를 `field`가 가리키는 행에 붙이는 것은 **Task 13**이다. 여기서는 엔진이 볼 수 없는
  것 하나(중복 이름)만 폼이 직접 말한다.

**검증**: `pnpm test` 348 passed (25 files), `typecheck`·`lint` clean, 실제 엔진에 붙은 브라우저에서
폼으로 스키마 작성 → 중첩 진입 → JSON 전환까지 확인.

---

### Task 12 — 자동 저장과 충돌

`store/save.ts`(디바운스·상태·충돌), `lib/engine/save.ts`(프록시 경유 PUT),
`lib/dsl/read.ts`(남이 쓴 초안 읽기), `components/save/{StatusBar,ConflictDialog}.tsx`,
그래프 스토어의 `replaceDocument`, 그리고 `app/page.tsx`가 워크플로를 연다.

**계획이 또 없는 것을 전제했다 — 이번엔 워크플로 id다.** Task 10이 없는 validation 슬라이스를 읽으려 했듯,
Task 12는 저장할 워크플로가 있다고 전제하는데 그것은 Task 19(목록)에서 생긴다. **실행되지 않는 자동
저장은 자동 저장이 아니므로** 여는 것까지 했다: URL의 `?workflow=`, 없으면 가장 최근 것, 그것도 없으면
새로 만든다. Task 19가 이 세 줄을 진짜 목록으로 바꾼다. 이 id는 Task 13의 `/validate`도 쓴다.

**브라우저로 열자마자 편집기가 터졌다.** `Cannot read properties of undefined (reading 'x')` —
DB에 있던 초안들은 **`position`이 없다.** 당연한 일이다: 위치는 에디터 전용이고 엔진은 `dsl_hash`에서
제외한다. API로 만든 초안, 예전 에디터가 쓴 초안, 앞으로 나올 에디터가 쓴 초안 모두 위치가 없을 수 있다.
**이건 예외가 아니라 정상이다.**

그래서 `readDocument`를 만들었고, 결과가 **둘뿐이다.**

- 읽히면: 위치를 채우고(겹치지 않게 격자로 — 그 다음 자동 정렬이 제대로 배치한다), **나머지는 전부
  그대로** 통과시킨다. `config`·`policy`·`label`·`settings`, 그리고 새 엔진이 추가한 키까지.
- 안 읽히면: **거부한다.** 그리고 호출자는 편집기를 열지 않는다.

거부가 왜 중요한가. 못 읽은 초안 위에 빈 문서로 편집기를 열면 **1초 안에 자동 저장이 그 빈 문서를
테넌트의 초안 위에 덮어쓴다.** 거부는 편집 한 번을 잃고, 추측은 워크플로를 잃는다. 같은 이유로 읽을 수
없는 노드를 **버리지 않고** 거부한다 — 버린 노드는 다음 자동 저장에서 사라지고, 어느 것이었는지 아무도
모른다.

**충돌은 절대 자동으로 풀지 않는다.** 409면 자동 저장이 **멈추고** 대화상자가 뜬다. 되묻지 않는 재시도는
두 탭이 서로를 덮어쓰는 경쟁이고, 거기서는 마지막에 친 사람이 이기고 다른 쪽 작업은 사라진다. 사람의
클릭이 **그 한 번의 재시도**이고(3 설계 §9), 두 번째 409는 루프 대신 다시 묻는다.
버튼 둘 다 **무엇을 잃는지** 적었다 — 이름만 있는 버튼 둘 중 하나를 고르는 것은 동전 던지기다.

`불러오기`는 히스토리를 **버린다**(`replaceDocument`). 되돌리기가 교체 이전으로 닿으면 방금 포기하기로
한 내 초안이 되살아나고, 자동 저장이 그것을 상대 초안 위에 쓴다 — 사람이 거절한 바로 그 덮어쓰기다.

**jsdom에는 `showModal`이 없다.** 셋업에 shim을 넣되, **그 shim이 무엇을 검사하지 못하는지 주석에
적었다**: 포커스 가둠, 배경 비활성, Esc는 jsdom에 아예 없고 그것들이 바로 파괴적 선택을 든 대화상자에서
중요한 부분이다. 그래서 브라우저 테스트(`e2e/conflict-dialog.spec.ts`)를 따로 뒀고, 대조군 둘
(`onCancel` 제거, `showModal`→`show`)이 각각 실패하는 것을 확인했다.

그 브라우저 테스트가 **대화상자가 왼쪽 위 구석에 처박혀 있는 것**도 잡았다. UA 스타일시트는 모달을
`margin: auto`로 가운데 놓는데, Tailwind preflight가 모든 요소의 margin을 0으로 만든다.

**`Date.now()`를 렌더 중에 읽고 있었다.** lint가 순수하지 않다고 잡았고, 맞는 지적이다 — 게다가 그 시각은
**갱신되지 않는다.** 다른 무언가가 다시 렌더할 때까지 "방금"이라고 적혀 있게 된다. 30초마다 도는 시계를
넣었다(분 경계에서만 글자가 바뀌므로 더 자주 돌 이유가 없다). 테스트는 자기 시각을 넘긴다.

**화면에 `role="status"`가 둘이 됐다** — 캔버스의 편집 오류와 저장 상태. 이름 없는 live region 둘은
스크린 리더에게 구분되지 않고, 테스트에게도 구분되지 않았다(그래서 발견했다). 저장 쪽에
`aria-label="저장 상태"`를 붙였다.

**Mutation 22/22 잡힘** (둘은 테스트 추가 후, 하나는 코드 삭제로)

| 변형 | 결과 |
|---|---|
| 미해결 충돌 위에 계속 저장 | killed |
| 저장 중 변경이 두 번째 저장을 시작 / 잊힘 | killed |
| 충돌이 대화상자를 안 염 | killed |
| 디바운스 타이머를 취소하지 않음 | killed |
| `불러오기`가 문서를 교체 안 함 / 낡은 revision 유지 | killed |
| 모든 응답을 성공으로 취급 | killed |
| 실패가 엔진 메시지를 잃음 | killed |
| 409를 인식 못 함 / 쓸 수 있는 409를 실패로 강등 | killed |
| **details 타입이 틀린 409를 충돌로 취급** | 처음엔 survived → 테스트 추가 후 killed |
| revision 없는 200을 수용 | killed |
| 워크플로 id를 경로에 그대로 붙임 | killed |
| 위치 없음/반쪽을 그대로 둠 | killed |
| 못 읽는 노드를 버림 | killed |
| 채운 위치가 서로 겹침 | killed |
| `settings`와 모르는 키를 버림 | killed |
| `replaceDocument`가 히스토리를 유지 | killed |
| **후속 저장 대기 중 상태 표시** | survived → **관측 불가능한 죽은 분기여서 삭제** |
| `덮어쓰기`가 진 revision으로 재전송 | survived (등가 변형) |

마지막 둘이 기록할 값이 있다.

- **"후속 저장 대기 중"** 분기는 `again ? "pending" : "saved"`였는데, 바로 다음 줄의 후속 `send`가
  **같은 동기 구간에서** `"saving"`으로 덮는다. 아무도 볼 수 없는 상태다. 테스트를 억지로 만드는 대신
  분기를 지우고, 대신 **관측 가능한** 보장을 테스트했다: 후속 저장이 날아가는 동안 상태 표시줄은
  `저장 중`이지 `저장됨`이 아니다.
- **`덮어쓰기`**는 재전송 직전에 `revision`을 `currentRevision`으로 이미 올려두므로
  `send(conflict.currentRevision)`과 `send(get().revision)`이 같은 값이다. 등가 변형이라 그대로 뒀다.

**실제 엔진으로 확인.** 편집 → `저장 대기 중` → `저장됨 마지막 저장 방금`, PUT 한 번 200.
탭 둘을 같은 워크플로에 띄워 실제 409를 만들었고, 대화상자가 뜨고 `덮어쓰기`가 이긴 revision으로
재전송해 `저장됨`으로 끝나는 것까지 봤다.

**남은 공백.**
- **창을 닫을 때 저장 대기 중인 변경을 흘린다.** `beforeunload`로 `flush`를 부르거나 경고를 띄우는 것이
  맞는데, 계획에 없고 Task 21(E2E)에서 실제로 확인할 수 있는 형태로 넣는 편이 낫다고 판단했다.
- 워크플로 **이름**을 바꾸는 UI가 없다. `PUT`은 `name`을 받지만 Task 19의 몫이다.

**검증**: `pnpm test` 401 passed (29 files), `e2e` 10 passed, `typecheck`·`lint` clean,
`build` 성공, `test:bundle` 통과.

---

### Task 13 — 검증 표시와 한국어 메시지

`store/validation.ts`(슬라이스와 조회 함수들), `lib/engine/validate.ts`, `lib/errors/korean.ts`,
`components/validation/{Badge,Banner,RunButton}.tsx`, 그리고 `flow.ts`·`WorkflowNode`·`NodePanel`·
`Canvas`가 그것을 쓴다. 엔진에는 테스트 파일 하나(`tests/test_pydantic_messages.py`)만 추가했다.

**계획이 없는 것을 세 번째로 전제했다 — 이번엔 pydantic의 오류 *타입*이다.** 계획은
`string_too_short`·`literal_error` 같은 타입으로 매핑하라고 했는데,
`engine/validator/structure.py::pydantic_issues`는 `err["msg"]`만 보낸다. **타입은 전선을 건너지 않는다.**

선택지가 둘이었다. 엔진이 타입을 싣게 바꾸거나, 웹이 문구로 매칭하거나. **문구로 매칭하기로 했다** —
엔진의 공개 이슈 계약을 웹 편의를 위해 넓히지 않으려고. 문구 매칭의 약점은 하나뿐이다: pydantic이
문장을 바꾸면 조용히 번역이 멈춘다. 그래서 **정확한 문자열들을 엔진 테스트 스위트에 못 박았다**
(`tests/test_pydantic_messages.py`) — pydantic이 실제로 사는 곳이고, 업그레이드가 거기서 시끄럽게
깨진다. 웹 테스트는 pydantic을 볼 수 없으므로 거기서는 못 박을 수가 없다.

그리고 실패 모드 자체가 안전하다: 모르는 문장은 **영어 원문 그대로** 보여준다. 읽기 불편하지만 틀리지는
않는다. 번역이 없다고 메시지를 비우면 고칠 수 있었던 문제가 조용한 실패가 된다.

**반만 번역된 문장을 하나 잡았다.** `Input should be 'a' or 'b'`를 그대로 넣으니
`'a' or 'b' 중에서 골라야 합니다`가 나왔다. 두 언어가 섞인 문장은 어느 한 언어로 된 문장보다 읽기 나쁘다.
` or `를 ` 또는 `로 바꿨다.

**`nodes`는 비어 있는 응답에 지워지지 않는다.** 이게 이 슬라이스의 핵심이다(3 설계 §4.1). 구조 오류 —
중복 노드 id, 순환 — 가 있으면 분석 단계에 도달하지 못해서 `/validate`가 이슈만 주고 `nodes`는 빈 채로
답한다. 거기서 맵을 갈아치우면 **오류를 고치려고 편집하는 바로 그 순간 템플릿 자동완성 목록이 빈다.**
조금 낡은 스키마는 조금 틀린 것이고, 빈 목록은 쓸모가 없다. `undefined`와 `{}` 둘 다 "유지"로 다룬다.

같은 이유로 **요청 자체가 실패하면 이슈를 지우지 않는다.** 배지를 지우는 것은 "문제 없음"이라고 말하는
것인데, 진실은 "모른다"이다.

**답이 순서를 어겨 도착할 수 있다.** 타이핑 중에는 요청 둘이 동시에 떠 있는 것이 정상이고, 느린 쪽이
이기면 안 된다. 티켓 번호로 가장 최근 것만 쓰게 했다.

**배지는 색만으로 말하지 않는다.** 빨간 점과 노란 점은 색각 이상이 있는 사람에게 같은 점이다. 글리프
(`!`/`?`)와 개수가 같은 정보를 나른다. 메시지가 여럿이면 **첫 문장 + "외 N건"** 이다 — 다섯 문장을 이어
붙인 `title`은 아무도 읽지 않는다.

**`handles.<name>`은 필드가 아니다.** `HANDLE_NOT_CONNECTED`는 분기 출력 하나를 가리킨다. 노드만
표시하면 "여기 뭔가 잘못됐다"이고, 핸들을 표시하면 "이걸 연결해라"이다 — 후자가 그 오류의 내용 전부다.
`fieldOf`와 `handleOf`를 따로 두고, 서로를 오해하는 변형 둘을 각각 테스트가 잡는다.

**`LIMIT_EXCEEDED`는 배너로 간다.** 문서 크기는 어느 노드의 사실도 아니다. 아무 노드에나 배지를 달면
멀쩡한 노드를 고치러 보내게 된다.

**실행 버튼을 지금 만들었다.** 동작은 Task 14가 준다. 여기 있는 이유는 **실행을 막는 것이 검증 질문이기
때문이다**: 오류가 있으면 엔진이 어차피 거절하고, 누르고 나서 알게 되는 것은 누름을 낭비하고 버튼이
못 미덥다는 인상을 남긴다. 막는 이유는 `title`에만 두지 않고 `aria-describedby`로도 읽힌다 — 툴팁은
터치 화면에 존재하지 않고 모든 스크린 리더가 읽지도 않는다. 경고는 막지 않는다: 막으면 경고와 오류가
구분되지 않는다.

**Mutation 19/19 잡힘** (넷은 테스트 추가 후)

| 변형 | 결과 |
|---|---|
| 빈 분석이 유지된 맵을 대체 / null로 | killed |
| 낡은 응답이 최신을 덮어씀 | killed |
| 실패한 요청이 이슈를 지움 | killed |
| `worstOf`가 경고를 못 봄 / 경고를 오류보다 위로 | killed |
| 엣지 이슈를 워크플로 전체로 분류 | killed |
| 경고를 오류로 셈 | killed |
| 핸들 경로를 설정 필드로 / 그 반대 | killed |
| 모르는 문장이 빈 문자열이 됨 | killed |
| 가장 흔한 문장을 번역 안 함 | killed |
| **마지막 콜론에서 분리** | 처음엔 survived → 테스트 추가 후 killed |
| **열거가 반만 영어로 남음** | 처음엔 NO-OP(하네스) → 다시 돌려 killed |
| **엣지마다 아무 이슈로 색칠 / 노드마다 모든 이슈 / 핸들 아닌 것이 핸들 이름으로** | 처음엔 survived → 테스트 추가 후 killed |
| 빈 캔버스가 실행 가능 / 오류가 실행을 안 막음 | killed |

첫 번째 생존자가 기록할 값이 있다. 원래 테스트는 콜론이 든 한국어 메시지를 썼는데, 뒤쪽 조각이 어떤
규칙과도 안 맞아서 느슨한 정규식으로도 결과가 같았다 — **통과했지만 아무것도 검사하지 않았다.**
구분되는 입력은 **뒤쪽 조각이 규칙과 맞는 경우**다: `설정 오류: Value error, 시간: 형식이 ...`.
마지막 콜론에서 자르면 `Value error,` 규칙에 닿지 못하고, 엔진이 가장 신경 써서 쓴 메시지에서만
번역이 조용히 멈춘다.

**실제 엔진으로 확인.** 저장돼 있던 워크플로를 열자 LLM 노드 둘이 빨간 테두리와 `2` 배지를 달았고,
실행 버튼이 `오류 4건을 먼저 해결해 주세요`로 비활성화됐으며, 패널의 모델·프롬프트 칸 아래에
`설정 오류: 반드시 입력해야 합니다`가 붙었다 — pydantic의 영어가 아니라.

**남은 공백.**
- **경고를 끄거나 접는 수단이 없다.** `REF_NOT_GUARANTEED`가 많은 워크플로에서는 노란 배지가 배경 소음이
  된다. 계획에 없어서 넓히지 않았다.
- 검증 디바운스가 **700ms**로 자동 저장(1s)과 따로 돈다. 한 문서 변경에 요청이 둘 나간다는 뜻이다.
  Task 14 이후 실제로 부담이 되는지 보고 합치는 편이 낫다고 판단했다.

**검증**: `pnpm test` 462 passed (35 files), `e2e` 10 passed, `typecheck`·`lint` clean,
`build` 성공, `test:bundle` 통과.

---

### Task 14 — 실행 시작

`lib/engine/run.ts`(네 갈래 응답), `lib/run/inputs.ts`(시작 노드의 입력 스키마), `store/run.ts`,
`components/run/RunDialog.tsx`, 그리고 캔버스의 `?run=` 동기화.

**계획이 잘못 짚은 것 둘.**

1. **"Task 11의 렌더러를 read-and-fill 모드로 재사용"은 할 수 없다.** Task 11의 렌더러는 *스키마를
   편집한다*. 실행 대화상자가 하는 일은 그 스키마에 맞춰 *값을 채우는* 것이고, 그건 RJSF가 이미 하는
   일이다 — 설정 패널이 노드 타입의 `configSchema`에 대해 하는 것과 같은 일이다. 스키마 편집기를 여기
   재사용했다면 **폼 엔진을 하나 더 쓰는 셈**이었다. RJSF를 썼고 Task 9의 템플릿을 그대로 쓴다.
2. **실행의 409는 저장의 409와 모양이 다르다.** 계획은 "Task 12의 충돌 대화상자를 보여준다"고 했는데,
   `create_run`의 409는 `details.currentRevision`만 싣는다 — `draftDsl`이 없다. 그 대화상자는 **두 초안
   중 하나를 고르는** 것이므로, 상대 초안이 없으면 고를 것이 없다. `GET /workflows/{id}`로 따로 가져온다.
   가져오지 못하면 `draftDsl: null`로 보고한다 — 덮어쓰기는 제안할 수 있고 불러오기는 못 하는 상태가,
   아무 일 없는 척하는 것보다 낫다.

**멱등 키는 제출마다 새로 만든다.** 엔진은 이 키로 중복을 제거한다(`run_db.find_by_idempotency_key`).
그것이 **재시도된 요청**을 안전하게 만드는 장치인데, 키를 재사용하면 **의도적인 두 번의 실행이 하나로
합쳐진다.** 더블클릭은 한 번 실행되어야 하고, 일부러 두 번 누른 것은 두 번 실행되어야 한다 — 둘은 다른
일이다. 테스트가 두 제출의 키가 다르다는 것과 그것이 UUID라는 것을 각각 단언한다.

스토어는 **진행 중일 때 두 번째 누름을 무시한다.** 느린 네트워크에서의 조급한 더블클릭과 의도적인 두
번째 실행을 스토어는 구별할 수 없고, 구별할 수 없을 때는 안전한 쪽으로 읽는다.

**422는 배지로 돌려보낸다.** 검증이 낡을 수 있다 — 에디터의 마지막 `/validate`는 깨끗했는데 엔진의
것은 아닌 경우다. 그때 받은 `details.issues`를 Task 13의 이슈 목록에 합쳐서 **캔버스의 배지로** 띄운다.
대화상자 안의 문장 하나보다, 고칠 수 있는 자리에 있는 편이 낫다.

**대화상자의 검증은 표시용이 아니다.** 설정 패널은 반쯤 친 값 때문에 편집을 막지 않지만(엔진이 권위),
실행은 다르다 — 필수 입력이 빠진 실행을 보내면 왕복 한 번을 버리고 오류가 읽기 더 어려운 곳에 뜬다.
그래서 여기서는 RJSF의 검증이 제출을 막는다. Esc로 닫히는 것도 저장 충돌 대화상자와 다르다:
여기서 닫는다고 잃는 것이 없다.

**`?run=`은 `replaceState`로 넣는다**(3 설계 §8.3). 실행은 페이지를 바꾸지 않았고, 히스토리 항목을
쌓으면 브라우저 뒤로 가기가 실행을 취소하는 것처럼 보인다 — 취소할 수 없는데도.

**lint가 진짜 버그를 하나 잡았다.** `issues`를 병합한 뒤 `useMemo`의 의존성만 바꾸고 **본문은
`validation.issues`로 남겨뒀다.** 422의 이슈가 패널과 엣지에는 가고 **노드 배지에는 영영 안 가는**
상태였다. exhaustive-deps 경고가 아니었으면 브라우저에서 422를 일부러 만들기 전까지 몰랐을 것이다.

**Mutation 17/17 잡힘**

| 변형 | 결과 |
|---|---|
| 멱등 키 재사용 / 미전송 | killed |
| 형식이 틀린 202 수용 | killed |
| 409·422를 인식 못 함 (각각) | killed |
| revision 없는 409를 충돌로 | killed |
| 상대 초안을 안 가져옴 | killed |
| 422가 이슈를 잃음 | killed |
| 워크플로 id 미이스케이프 | killed |
| 스키마가 아닌 config를 스키마로 / 빈 스키마를 선언으로 | killed |
| 아무 노드의 `inputs`나 읽음 | killed |
| 프로퍼티 없는 스키마가 입력을 물음 | killed |
| 진행 중 두 번째 누름이 또 실행 | killed |
| 이전 시도의 불만을 안 지움 | killed |
| 실행 id를 버림 / 거절이 이슈를 잃음 | killed |

**실제 엔진으로 확인.** 실행 버튼 → 대화상자 → `issue`에 "이슈 42" 입력 → 202,
URL에 `?run=7b476af8-...`. 엔진 쪽에서 `start` 노드가 `{"issue":"이슈 42"}`로 성공한 것까지 확인했다 —
한글이 그대로 건너갔다. 그 다음 `http_1`이 그 문자열을 URL로 써서 `HTTP_BLOCKED`로 실패했는데,
이건 그 워크플로의 내용과 내가 넣은 엉터리 입력 탓이지 에디터 탓이 아니다.

**확인 과정에서 만든 흔적을 지웠다.** Task 12 검증 때 실제 워크플로에 남긴 LLM 노드 둘을 지우고
저장했다 — 내가 실수로 넣은 것이라 원래대로 되돌린 것이고, 그래야 검증이 통과해 실행을 시험할 수 있었다.

**남은 공백.**
- **실행이 시작된 뒤 아무 일도 일어나지 않는다.** 노드 색도, 진행도, 실패 표시도 없다 — 전부 Task 15의
  이벤트 스트림이 한다. 지금은 URL에 `?run=`이 붙는 것이 유일한 증거다.
- **`?run=`으로 다시 들어와도 아무것도 복원되지 않는다.** Task 18의 몫이다.

**검증**: `pnpm test` 497 passed (39 files), `e2e` 10 passed, `typecheck`·`lint` clean,
`build` 성공, `test:bundle` 통과.
