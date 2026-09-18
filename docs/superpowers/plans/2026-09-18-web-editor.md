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

- [ ] `pnpm create next-app apps/web` with TypeScript, ESLint, App Router, no Tailwind decision yet — pick Tailwind, it keeps component CSS out of this plan's way.
- [ ] Set `"strict": true`, `"noUncheckedIndexedAccess": true` in `tsconfig.json`. The second one matters: the editor indexes into `nodes[id]` constantly and TypeScript should force the missing case to be handled.
- [ ] Add vitest with `environment: "jsdom"` for component tests and `environment: "node"` for `lib/` tests — use vitest projects so both run under one `pnpm test`.
- [ ] Add scripts: `dev`, `build`, `start`, `test`, `test:watch`, `lint`, `typecheck`, `e2e`.
- [ ] Write one trivial test in `lib/` and one component test, so both projects are proven to run.
- [ ] Add `apps/web` to the repo `.gitignore` exceptions as needed (`node_modules`, `.next`).

**Verification**
- [ ] `pnpm test` passes with two tests.
- [ ] `pnpm typecheck` and `pnpm lint` pass.
- [ ] `pnpm build` succeeds.
- [ ] Commit: `chore(web): scaffold the Next.js editor app`

---

## Task 2: The BFF proxy

The only place `ENGINE_API_TOKEN` is used. Get this wrong and either the editor cannot talk to the engine or the token leaks.

**Tests first** (`lib/engine/paths.test.ts`, `app/api/engine/route.test.ts`):

- [ ] `isAllowed("/workflows")`, `/workflows/<uuid>`, `/workflows/<uuid>/runs`, `/runs/<uuid>/events`, `/node-types`, `/secrets`, `/healthz` → true.
- [ ] `isAllowed("/openapi.json")`, `/docs`, `/redoc`, `/` → false. A blocked path gets 404, not 403 — do not confirm what exists.
- [ ] Path traversal: `/workflows/../openapi.json` and its percent-encoded forms are rejected. Assert on the **normalised** path, and write a test for `%2e%2e%2f` specifically.
- [ ] The proxy adds `Authorization: Bearer <token>` — asserted by inspecting the upstream `fetch` call, not by hitting a real engine.
- [ ] An engine 409 with an error envelope comes back **byte-identical** with status 409. Assert on the parsed body: `details.currentRevision` survives.
- [ ] `Last-Event-ID: 42` on the incoming request appears on the upstream request.
- [ ] `Idempotency-Key` is forwarded.
- [ ] Hop-by-hop and dangerous request headers are **not** forwarded: `host`, `connection`, `content-length`, and any client-supplied `authorization` (a caller must not be able to override the token).
- [ ] A streaming response's `body` is passed through without being read.
- [ ] `request.signal` is passed to the upstream fetch (Task 0's question 4).

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

- [ ] `responseHeaders` copies `content-type`, `cache-control`, and drops `content-encoding`/`transfer-encoding` (fetch already decoded the body; re-advertising the encoding corrupts it).
- [ ] `isAllowed` works on a **normalised** path. Reject any segment equal to `.` or `..` after decoding, and reject a path that is not one of the seven prefixes.
- [ ] Env validation at module load: if `ENGINE_API_URL` or `ENGINE_API_TOKEN` is missing, throw with a message naming the variable. Failing at startup beats 401s nobody can explain.

**The token-leak test** — this is the one that earns its keep:

- [ ] After `pnpm build`, grep `.next/static/**/*.js` for the token value used in the build. Assert zero matches. Run it as part of `pnpm test` behind a flag, or as a separate `pnpm test:bundle` invoked by Task 22's final verification.

**Verification**
- [ ] All proxy tests pass; the traversal test fails first for the right reason.
- [ ] Commit: `feat(web): proxy engine requests server-side with the shared token`

---

## Task 3: `/validate` returns what autocomplete needs (engine)

3 설계 §4.1. Without this the editor either has no autocomplete or reimplements `compute_before` — and a second implementation of the guaranteed-set rule will disagree with the engine on exactly the graphs that are hard.

**Tests first** (`services/engine/tests/test_api_validate.py`):

- [ ] A valid chain `start → template_1 → end` returns `nodes` with `template_1.variables == ["start"]` and `end.variables == ["start", "template_1"]`.
- [ ] A branch: nodes only on one side of a condition are **not** in the join's `variables` (the ∩ rule), and a merge's are (the ∪ rule).
- [ ] `handles` for a `condition` is `["true", "false"]`; for a `classifier` it is its category ids plus `default`; for `human_approval` it is `["approve", "reject"]`.
- [ ] `outputSchema` for an `llm` node without `outputSchema` config is the text schema (`{text: string}`).
- [ ] A DSL with a structural error returns `issues` **and** `"nodes": {}` — not a 500, and not a partial graph.
- [ ] A DSL with only warnings returns both `issues` and a full `nodes`.
- [ ] `variables` is sorted, so the response is stable between calls (a set's iteration order is not).
- [ ] 201 nodes → `nodes` has 200 entries and `nodesTruncated` is `true`. 200 nodes → no `nodesTruncated` key.
- [ ] The existing `/validate` tests still pass unchanged: adding a key must not change `issues`.

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

- [ ] `validate_workflow` calls it and adds `nodes` (and `nodesTruncated` only when true).
- [ ] `compute_before`/`compute_schemas` are already imported by `engine.validator`; export them from there rather than reaching into `engine.validator.refs` from the router.
- [ ] **Do not** recompute the analysis. `analyze()` already ran; these two functions are cheap re-walks of the graph it returned, but calling `analyze` twice would double the CPU on the editor's hottest endpoint.

**Mutation test**
- [ ] Change `sorted(...)` to `list(...)` — a test must fail (stability).
- [ ] Change `[:MAX_ANALYSIS_NODES]` to no slice — the truncation test must fail.
- [ ] Make `_node_analysis` return `{}` unconditionally — several tests must fail.
- [ ] Any survivor is either a missing test or genuinely equivalent; write down which.

**Verification**
- [ ] `uv run pytest -q` and `uv run ruff check .` pass.
- [ ] Commit: `feat(engine): return per-node variables, schemas and handles from /validate`

---

## Task 4: Normalise the `http_request` category (engine)

- [ ] Write a test asserting every registered node's `category` is one of `{"IO", "Logic", "AI", "Human", "Action"}`. It fails on `http_request`'s `"action"`.
- [ ] Change `HttpRequestNode.category` to `"Action"`.
- [ ] Grep the engine tests for the literal `"action"` and fix any that pinned the old value.
- [ ] `uv run pytest -q` and `uv run ruff check .` pass.
- [ ] Commit: `fix(engine): give http_request the same category casing as every other node`

---

## Task 5: The DSL document and its commands

The core of the editor. Pure TypeScript, no React, no network — so it can be tested exhaustively and fast.

**Tests first** (`lib/dsl/commands.test.ts`, `lib/dsl/ids.test.ts`):

- [ ] `nextId("llm", dsl)` returns `llm_1` on an empty document, `llm_3` when `llm_1` and `llm_2` exist, and **`llm_3` when only `llm_2` exists** (max + 1, not count + 1).
- [ ] Deleting `llm_2` then adding an llm gives `llm_3`, never `llm_2` again.
- [ ] `start` and `end` get no number and `addNode` refuses a second one.
- [ ] Every command returns a **new** object; the input is not mutated (assert with a deep-frozen input).
- [ ] `removeNode` removes the node **and every edge touching it**.
- [ ] `connect` refuses a duplicate `(source, sourceHandle, target)` triple — the engine reports `EDGE_DUPLICATE` for it, but the editor should not create it in the first place.
- [ ] `connect` generates an edge id that does not collide with an existing one.
- [ ] `setPolicy(node, {})` removes the `policy` key entirely rather than writing `{}` (3 설계 §5.4 — `dsl_hash` treats them differently).
- [ ] `setConfig` replaces the whole config object; it does not deep-merge. A merged update cannot delete a key.

**Implementation**
- [ ] `lib/dsl/document.ts`: `EditorDsl = WorkflowDSL & { nodes: (Node & { position: XY })[] }`. Position is editor-only and excluded from `dsl_hash` by the engine, so it rides along in the document.
- [ ] `lib/dsl/commands.ts`: one exported function per command, each `(dsl: EditorDsl, …args) => EditorDsl`.
- [ ] `lib/dsl/ids.ts`: `nextId` and `nextEdgeId`.

**Verification**
- [ ] `pnpm test` passes; the immutability test fails first if a command mutates.
- [ ] Commit: `feat(web): model the workflow document with pure edit commands`

---

## Task 6: Undo/redo

**Tests first** (`lib/dsl/history.test.ts`):

- [ ] Apply three commands, undo twice, redo once → the document equals the state after the second command.
- [ ] A new command after an undo **clears the redo stack**.
- [ ] The stack is capped at 50; the 51st push drops the oldest and undo still works 50 times.
- [ ] Consecutive `setPosition` on the **same node** merge into one entry; on different nodes they do not.
- [ ] `commit()` ends a merge window, so a drag followed by a config change is two entries.
- [ ] Undo restores positions exactly (a merged drag undoes to before the drag, not to an intermediate frame).

**Implementation**
- [ ] `history.ts` keeps `past: EditorDsl[]`, `future: EditorDsl[]` and a `mergeKey: string | null`. Snapshots, not inverse operations (3 설계 §5.1).
- [ ] `push(dsl, mergeKey?)`: when `mergeKey` equals the previous push's key, replace the top of `past` instead of adding to it — **no**, the opposite: keep the *older* snapshot (that is the state to return to) and do not push a new one.

Write that last point as a test before implementing it; getting the direction backwards is the obvious bug and it looks fine until you undo a drag.

**Verification**
- [ ] `pnpm test` passes.
- [ ] Commit: `feat(web): undo and redo over document snapshots`

---

## Task 7: Palette and canvas

- [ ] Server component fetches `/node-types` through the proxy and passes it down.
- [ ] Palette groups by `category` in a fixed order: `IO`, `AI`, `Logic`, `Action`, `Human`. An unknown category goes last under "기타" rather than disappearing.
- [ ] Drag from the palette onto the canvas → `addNode` at the drop position.
- [ ] React Flow renders nodes from the document: label, type icon, id in small grey text, handles from the node's `handles` (from the validation slice; falls back to `["out"]` before the first analysis).
- [ ] Connecting two handles calls `connect`; React Flow's own `onConnect` never mutates its internal state directly.
- [ ] Delete key removes the selection through `removeNode`/`removeEdge`.
- [ ] Node drag end calls `setPosition` with a merge key of `position:<nodeId>`.

**Component tests**
- [ ] The palette renders five groups in order with nine node types total.
- [ ] Dropping a `llm` on an empty canvas produces a document with `llm_1`.
- [ ] Deleting a node with two edges leaves zero edges.

**Verification**
- [ ] `pnpm test`, `pnpm typecheck` pass.
- [ ] Commit: `feat(web): canvas with a node palette and edge editing`

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

