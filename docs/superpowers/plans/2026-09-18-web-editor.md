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

- [ ] ELKjs (`elkjs/lib/elk.bundled.js`) with `layered` algorithm, left-to-right.
- [ ] Run it in a **Web Worker**. ELK on a 200-node graph blocks the main thread long enough to drop the drag the user is in the middle of.
- [ ] "자동 정렬" button applies the result as **one** `autoLayout` command, so one undo puts every node back.
- [ ] Test: a three-node chain gets strictly increasing x positions and the node count is unchanged.
- [ ] Test: layout on an empty document is a no-op that does not push a history entry.

**Verification**
- [ ] Commit: `feat(web): auto-layout the graph with ELK`

---

## Task 9: The settings and policy tabs

- [ ] Selected node → right panel with tabs 설정 / 실행 정책 / 라벨. The policy tab renders only when `defaultPolicy !== null`.
- [ ] RJSF with `validator-ajv8`, `schema` = the node type's `configSchema`, `formData` = the node's config.
- [ ] **RJSF's own validation is display-only.** The engine is the authority; do not block editing on an ajv error. Set `liveValidate={false}` and `noHtml5Validate`.
- [ ] `uiSchema` table per node type supplies Korean labels and field order. Keep it in `components/panel/uiSchema.ts` with a comment saying why it cannot come from the engine.
- [ ] Widget registry: `x-template` → the (still stub, Task 10) template widget; `start.inputs` and `llm.outputSchema` → the (still stub, Task 11) schema widget.
- [ ] Policy tab writes `policy` only when a field differs from `defaultPolicy`; clearing the last override removes the key (Task 5's test already covers the command).
- [ ] `http_request` with method POST or PATCH: `maxAttempts` disabled with the Korean note from 3 설계 §5.4.
- [ ] Changes are debounced into `setConfig` at 300ms so every keystroke is not a history entry.

**Component tests**
- [ ] Selecting a `template` node shows the 설정 tab and no 실행 정책 tab.
- [ ] Selecting an `llm` node shows both.
- [ ] Switching `http_request` method GET → POST disables the attempts input and shows the note.

**Verification**
- [ ] Commit: `feat(web): schema-driven settings and policy panels`

---

## Task 10: The template input

The hardest UI in this plan. 3 설계 §6.

**Tests first** (`lib/template/parse.test.ts`, `lib/template/complete.test.ts`):

- [ ] `ranges("a {{ b.c }} d")` returns one range with the right offsets and the inner text `b.c`.
- [ ] Unclosed `{{` produces no range (so a chip never swallows the rest of the document while typing).
- [ ] `{{` inside a Jinja comment or a `{% %}` block is not a reference.
- [ ] `candidates(prefix, variables, schemas)`:
  - after `{{ ` → one candidate per node in `variables`, plus the not-guaranteed ones marked
  - after `{{ llm_1.` → the properties of `llm_1`'s `outputSchema`
  - after `{{ llm_1.text` → no further candidates for a `string`
  - a node id that is not in `variables` still yields candidates, flagged `guaranteed: false`
- [ ] The insert text is the node **id**; the display label is the node **label**.

**Implementation**
- [ ] CodeMirror 6 in a controlled React wrapper. `EditorView` is created once; document changes are applied through transactions, never by recreating the view (recreating it loses IME composition and the cursor).
- [ ] `autocompletion({ override: [source] })` with the source built from the validation slice.
- [ ] `Decoration.replace` over each reference range with a chip widget — **except** when the selection intersects the range, so a chip under the cursor opens back into text.
- [ ] A `ViewPlugin` recomputes decorations on document and selection change.
- [ ] The collapsible Korean help text from 3 설계 §6.2, below the editor.

**IME test — do not skip this**
- [ ] A Playwright test (this one does need a browser) types Korean via `page.keyboard.insertText` **and** via a composition sequence, and asserts the document contains the composed text once, not twice, and that no chip decoration was applied mid-composition.
- [ ] If CodeMirror + IME + decorations fight, fall back per 3 설계 §12: drop the chips, keep autocomplete and highlighting. Record the decision in the post-review note.

**Verification**
- [ ] Commit: `feat(web): template editor with variable autocomplete`

---

## Task 11: The schema editor

- [ ] Form mode: a table of fields (이름, 타입, 필수, 설명). Types offered: 문자열, 숫자, 정수, 참/거짓, 목록, 객체 — the engine's accepted `type` values, no more.
- [ ] Nested object/array: one level at a time, with a breadcrumb.
- [ ] JSON mode toggle with a plain textarea, for someone who knows what they are doing.
- [ ] Switching form → JSON always works. JSON → form works only if the JSON is inside the supported subset; otherwise the toggle is disabled with a Korean explanation, and the JSON stays authoritative. **Never silently drop a keyword the form cannot represent.**
- [ ] Validation comes from `/validate` only (3 설계 §7). Do not reimplement `schema_problems`.

**Tests**
- [ ] Adding two fields and marking one required produces `{type: "object", properties: {...}, required: ["a"]}`.
- [ ] Loading a schema with `anyOf` disables the form toggle and keeps the JSON intact through a round trip.
- [ ] Removing the last field produces `{type: "object", properties: {}}`, not `{}`.

**Verification**
- [ ] Commit: `feat(web): a form editor for the engine's JSON Schema subset`

---

## Task 12: Autosave and the conflict dialog

**Tests first**
- [ ] A change schedules a save 1s later (fake timers). Three changes in 500ms produce **one** save.
- [ ] A change during an in-flight save produces exactly one follow-up save after it returns.
- [ ] 200 updates the stored revision.
- [ ] 409 opens the dialog and does **not** retry on its own.
- [ ] "불러오기" replaces the document with `details.draftDsl` and adopts `details.currentRevision`.
- [ ] "덮어쓰기" resends with `details.currentRevision`; a **second** 409 reopens the dialog rather than looping.
- [ ] A 500 leaves the document untouched, shows the failure in the status bar, and retries on the next change.
- [ ] The document is never replaced without the user choosing it.

**Implementation**
- [ ] `store/save.ts` holds `revision`, `status: "saved" | "saving" | "pending" | "conflict" | "error"`, and the debounce timer.
- [ ] The status bar shows 저장됨 / 저장 중 / 저장 실패 with the last successful time.

**Verification**
- [ ] Commit: `feat(web): debounced autosave with explicit conflict resolution`

---

## Task 13: Validation badges and Korean error messages

- [ ] After a successful save, call `/validate`; store `issues` and `nodes` in the validation slice.
- [ ] `nodes` empty (structural error) → keep the previous `nodes` for autocomplete, per 3 설계 §4.1. Write a test for exactly this.
- [ ] Error badge (red) / warning badge (yellow) on nodes and edges by `nodeId`/`edgeId`.
- [ ] `field` puts the message next to the matching input in the panel. `handles.<name>` addresses a handle, so a `HANDLE_NOT_CONNECTED` error highlights that handle on the canvas.
- [ ] Any error → 실행 button disabled with a tooltip counting the errors.
- [ ] `LIMIT_EXCEEDED` → top banner, not a node badge.
- [ ] `lib/errors/korean.ts`: map pydantic error types (`string_too_short`, `greater_than_equal`, `literal_error`, `extra_forbidden`, …) to Korean sentences. Unmapped → show the original text.

**Tests**
- [ ] Each mapped pydantic type renders its Korean sentence.
- [ ] An unmapped type renders the English original, not an empty string.
- [ ] A workflow with one error disables the run button; fixing it re-enables it.

**Verification**
- [ ] Commit: `feat(web): show validation issues on the canvas in Korean`

---

## Task 14: Starting a run

- [ ] 실행 button → dialog with a form generated from the `start` node's `inputs` schema (reuse Task 11's renderer in read-and-fill mode).
- [ ] Submit → `POST /workflows/{id}/runs` with `{inputs, revision}` and a **fresh UUID** `Idempotency-Key` per submission.
- [ ] 202 → put `?run=<runId>` in the URL (3 설계 §8.3) and open the run panel.
- [ ] 409 `REVISION_CONFLICT` → the draft moved under us; show the conflict dialog from Task 12.
- [ ] 422 `VALIDATION_FAILED` → render `details.issues` as badges; this can happen if validation is stale.

**Tests**
- [ ] Two rapid submissions send two different idempotency keys (the engine dedupes a *retried* request, not two deliberate runs).
- [ ] A failed submission does not put `?run=` in the URL.

**Verification**
- [ ] Commit: `feat(web): start a run from a generated input form`

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
