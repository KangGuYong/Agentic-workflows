# Ponytail 리팩터링 실행 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 확인된 중복과 죽은 의존성을 동작 변경 없이 걷어낸다(순감소 약 60줄, 런타임 의존성 -1).

**Architecture:** 동작을 보존하는 리팩터링이다. 새 기능 테스트를 쓰는 대신, 기존 테스트를 **변경 전 기준선**으로 기록하고
각 태스크 뒤에 같은 결과가 나오는지 확인한다. 태스크 하나가 커밋 하나다. 스펙:
`docs/superpowers/specs/2026-09-22-ponytail-refactor-design.md`

**Tech Stack:** Next.js 16 + React 19 + TypeScript 5 + zustand + @xyflow/react(vitest, eslint, playwright) / Python + uv(pytest)

**브랜치:** `refactor/ponytail-trim` (이미 생성됨, 스펙 커밋 `687e7f1` 포함)

---

## 파일 맵

| 파일 | 변경 | 책임 |
|------|------|------|
| `apps/web/lib/engine/envelope.ts` | 생성 | 엔진 에러 envelope에서 `message`/`details`를 읽는 단 한 곳 |
| `apps/web/lib/engine/resume.ts` | 수정 | 로컬 `Envelope`·`messageOf` 삭제 → import |
| `apps/web/lib/engine/run.ts` | 수정 | 로컬 `Envelope`·`detailsOf`·`messageOf` 삭제 → import |
| `apps/web/lib/engine/save.ts` | 수정 | 로컬 `ErrorEnvelope`·`messageOf` 삭제, `conflictOf`가 `detailsOf` 사용 |
| `apps/web/lib/engine/secrets.ts` | 수정 | 로컬 `messageOf` 삭제 → import |
| `apps/web/lib/engine/workflows.ts` | 수정 | 로컬 `Envelope`·`messageOf` 삭제 → import |
| `apps/web/components/canvas/Canvas.tsx` | 수정 | 선택 병합 함수화, store 래퍼 인라인, URL 함수 통합, `inputSchema` 1회 계산 |
| `services/engine/pyproject.toml`, `services/engine/uv.lock` | 수정 | `jsonschema` 제거 |

---

### Task 0: 기준선 기록

**Files:** 없음 (읽기만)

- [ ] **Step 1: web 기준선**

Run (`apps/web`에서):
```bash
pnpm test && pnpm typecheck && pnpm lint
```
Expected: 모두 통과. vitest 요약 줄(`Tests  N passed`)의 N을 적어 둔다.

- [ ] **Step 2: web 컴포넌트 e2e 기준선**

Run (`apps/web`에서):
```bash
pnpm e2e --project=component
```
Expected: 모두 통과(`conflict-dialog`, `node-selection`, `template-ime`). 브라우저가 없어 실패하면
`pnpm exec playwright install chromium` 후 재실행한다. 그래도 실행 불가하면 이 사실을 기록하고 Task 2 검증에서도 동일하게 표시한다.

- [ ] **Step 3: engine 기준선**

Run (`services/engine`에서):
```bash
uv run pytest -q
```
Expected: 요약 줄(`N passed, M skipped`)을 그대로 적어 둔다. DB가 없어서 skip이나 error가 나는 테스트도 있는데, 결과가 기준선과 같으면 된다.

기준선이 이미 빨간색이면 **멈추고 보고한다**. 이 리팩터링으로 고칠 대상이 아니다.

---

### Task 1: 에러 envelope 읽기를 한 곳으로

**Files:**
- Create: `apps/web/lib/engine/envelope.ts`
- Modify: `apps/web/lib/engine/resume.ts:20-27`, `run.ts:40-52`, `save.ts:12-27`, `secrets.ts:20-23`, `workflows.ts:19-26`
- Test: 기존 `apps/web/lib/engine/{resume,run,save,secrets,workflows}.test.ts` (fallback 메시지와 엔진 메시지 전달을 이미 검증함)

- [ ] **Step 1: `envelope.ts` 생성**

```ts
/** Reading the engine's error envelope, `{ error: { code, message, details } }`.
 *
 * Every client in this folder answers a failed request with the engine's own message when it sent one
 * and a local fallback when it did not -- an empty string counts as not sending one.
 */

interface Envelope {
  error?: { code?: unknown; message?: unknown; details?: unknown }
}

export function messageOf(body: unknown, fallback: string): string {
  const message = (body as Envelope | null)?.error?.message
  return typeof message === "string" && message !== "" ? message : fallback
}

export function detailsOf(body: unknown): Record<string, unknown> | null {
  const details = (body as Envelope | null)?.error?.details
  return typeof details === "object" && details !== null ? (details as Record<string, unknown>) : null
}
```

- [ ] **Step 2: `resume.ts`**

아래 블록(20-27행)을 지운다.
```ts
interface Envelope {
  error?: { code?: unknown; message?: unknown; details?: unknown }
}

function messageOf(body: unknown, fallback: string): string {
  const message = (body as Envelope | null)?.error?.message
  return typeof message === "string" && message !== "" ? message : fallback
}

```
1행 문서 주석 바로 아래에 import를 추가한다.
```ts
/** Answering an approval and cancelling a run (3 설계 §8.4, Task 17). */

import { messageOf } from "./envelope"

export type Decision = "approve" | "reject"
```

- [ ] **Step 3: `run.ts`**

아래 블록(40-52행)을 지운다.
```ts
interface Envelope {
  error?: { code?: unknown; message?: unknown; details?: unknown }
}

function detailsOf(body: unknown): Record<string, unknown> | null {
  const details = (body as Envelope | null)?.error?.details
  return typeof details === "object" && details !== null ? (details as Record<string, unknown>) : null
}

function messageOf(body: unknown, fallback: string): string {
  const message = (body as Envelope | null)?.error?.message
  return typeof message === "string" && message !== "" ? message : fallback
}

```
1-2행 import를 다음으로 바꾼다.
```ts
import type { EditorDsl } from "@/lib/dsl/document"
import type { Issue } from "@/store/validation"

import { detailsOf, messageOf } from "./envelope"
```

- [ ] **Step 4: `save.ts`**

12-27행을 다음으로 바꾼다.
```ts
function conflictOf(body: unknown): { currentRevision: number; draftDsl: EditorDsl } | null {
  const details = detailsOf(body)
  if (details === null) return null
  const { currentRevision, draftDsl } = details
  if (typeof currentRevision !== "number" || typeof draftDsl !== "object" || draftDsl === null) return null
  return { currentRevision, draftDsl: draftDsl as EditorDsl }
}
```
1-2행 import를 다음으로 바꾼다.
```ts
import type { EditorDsl } from "@/lib/dsl/document"
import type { SaveResult } from "@/store/save"

import { detailsOf, messageOf } from "./envelope"
```

- [ ] **Step 5: `secrets.ts`**

아래 블록(20-23행과 뒤의 빈 줄)을 지운다.
```ts
function messageOf(body: unknown, fallback: string): string {
  const message = (body as { error?: { message?: unknown } } | null)?.error?.message
  return typeof message === "string" && message !== "" ? message : fallback
}

```
6행 문서 주석이 끝난 바로 뒤, `export interface SecretSummary` 위에 추가한다.
```ts
import { messageOf } from "./envelope"

```

- [ ] **Step 6: `workflows.ts`**

아래 블록(19-26행과 뒤의 빈 줄)을 지운다.
```ts
interface Envelope {
  error?: { code?: unknown; message?: unknown }
}

function messageOf(body: unknown, fallback: string): string {
  const message = (body as Envelope | null)?.error?.message
  return typeof message === "string" && message !== "" ? message : fallback
}

```
1행 문서 주석 바로 아래에 추가한다.
```ts
/** Workflow list operations from the browser, through the BFF proxy (Task 19). */

import { messageOf } from "./envelope"
```

- [ ] **Step 7: 남은 복사본이 없는지 확인**

Run (`apps/web`에서):
```bash
grep -rn "function messageOf\|function detailsOf\|interface Envelope\|interface ErrorEnvelope" lib components store
```
Expected: `lib/engine/envelope.ts`의 세 줄만 나온다.

- [ ] **Step 8: 검증**

Run (`apps/web`에서):
```bash
pnpm test lib/engine && pnpm typecheck && pnpm lint
```
Expected: 모두 통과하고 `lib/engine` 테스트 수는 기준선 그대로.

- [ ] **Step 9: 커밋**

```bash
git add apps/web/lib/engine
git commit -m "refactor(web): one error-envelope reader for the engine clients" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `Canvas.tsx` 정리

**Files:**
- Modify: `apps/web/components/canvas/Canvas.tsx`
- Test: 기존 vitest 전체 + `pnpm e2e --project=component` (`node-selection.spec.ts`가 선택 병합, `conflict-dialog.spec.ts`가 save store를 다룬다)

- [ ] **Step 1: 선택 병합을 순수 함수 하나로**

`onNodesChange` 안의 다음 블록을
```ts
      const selected = changes.filter((change) => change.type === "select")
      if (selected.length > 0) {
        const chosen = new Set(state.selection.nodes)
        for (const change of selected) {
          if (change.selected) chosen.add(change.id)
          else chosen.delete(change.id)
        }
        state.select({ nodes: [...chosen], edges: state.selection.edges })
      }
```
이렇게 바꾼다.
```ts
      const chosen = toggled(state.selection.nodes, changes)
      if (chosen !== null) state.select({ nodes: chosen, edges: state.selection.edges })
```
`onEdgesChange` 본문을
```ts
    (changes: EdgeChange[]) => {
      const selected = changes.filter((change) => change.type === "select")
      if (selected.length === 0) return
      const chosen = new Set(state.selection.edges)
      for (const change of selected) {
        if (change.selected) chosen.add(change.id)
        else chosen.delete(change.id)
      }
      state.select({ nodes: state.selection.nodes, edges: [...chosen] })
    },
```
이렇게 바꾼다.
```ts
    (changes: EdgeChange[]) => {
      const chosen = toggled(state.selection.edges, changes)
      if (chosen !== null) state.select({ nodes: state.selection.nodes, edges: chosen })
    },
```
파일 끝(`ToolbarButton` 위)에 추가한다.
```ts
/** `ids` with React Flow's `select` changes applied, or `null` when there were none -- so a move or a
 * resize never re-selects anything. */
function toggled(ids: readonly string[], changes: readonly (RfNodeChange | EdgeChange)[]): string[] | null {
  const selects = changes.filter((change) => change.type === "select")
  if (selects.length === 0) return null
  const chosen = new Set(ids)
  for (const change of selects) {
    if (change.selected) chosen.add(change.id)
    else chosen.delete(change.id)
  }
  return [...chosen]
}
```
`filter`의 타입 좁히기가 되지 않아 `change.selected`에서 타입 에러가 나면, 조건을
`(change): change is Extract<RfNodeChange | EdgeChange, { type: "select" }> => change.type === "select"`로 명시한다.

- [ ] **Step 2: store 래퍼 3개 인라인**

`Editor` 안의 다음 줄들을
```ts
  const saveStore = useSaveStore(store, workflowId, initialRevision)
  const save = useStore(saveStore)
  const validationStore = useValidationStore(workflowId)
  const validation = useStore(validationStore)
  const runStore = useRunStore(workflowId)
  const run = useStore(runStore)
```
이렇게 바꾼다.
```ts
  // The save, validation and run slices, bound to this editor's document and workflow and created once,
  // like the graph store. Without a workflow id there is nothing to call -- the fixtures mount the canvas
  // that way -- so each request answers locally rather than hitting an endpoint that would 404, and the
  // status bar says so instead of claiming 저장됨. `/validate` just reports nothing.
  const [saveStore] = useState(() =>
    createSaveStore({
      revision: initialRevision,
      getDsl: () => store.getState().dsl,
      onReload: (dsl) => store.getState().replaceDocument(dsl),
      save: (body) => (workflowId === undefined ? Promise.resolve(NO_WORKFLOW) : saveDraft(workflowId, body)),
    }),
  )
  const save = useStore(saveStore)
  const [validationStore] = useState(() =>
    createValidationStore((dsl) =>
      workflowId === undefined ? Promise.resolve({ issues: [] }) : validateDraft(workflowId, dsl),
    ),
  )
  const validation = useStore(validationStore)
  const [runStore] = useState(() =>
    createRunStore((body) => (workflowId === undefined ? Promise.resolve(NO_WORKFLOW) : startRun(workflowId, body))),
  )
  const run = useStore(runStore)
```
`const NODE_TYPES = { workflow: WorkflowNode }` 바로 아래에 추가한다.
```ts
const NO_WORKFLOW = { outcome: "failed" as const, message: "열린 워크플로가 없습니다" }
```
그리고 `useSaveStore`, `useValidationStore`, `useRunStore` 세 함수를 문서 주석까지 통째로 지운다.
`GraphState`, `SaveState` 타입 import는 `Toolbar`가 계속 쓰므로 남긴다.

- [ ] **Step 3: `?run=` 처리를 함수 하나로**

`Editor`의 다음 줄을
```ts
  useRunInUrl(run.runId)
```
이렇게 바꾼다.
```ts
  useEffect(() => {
    if (run.runId !== null) setRunParam(run.runId)
  }, [run.runId])
```
다음 effect 안의 호출은
```ts
    if (restored.gone) clearRunFromUrl()
```
이렇게 바꾼다.
```ts
    if (restored.gone) setRunParam(null)
```
`clearRunFromUrl`과 `useRunInUrl`을 문서 주석까지 지우고, 그 자리에 다음을 넣는다.
```ts
/** Point `?run=` at `runId`, or drop it for `null` (3 설계 §8.3), so reloading the tab comes back to the
 * same run and a run the engine does not have stops being retried on every reload.
 *
 * `replaceState`, not a navigation: the run did not change which page this is, and pushing a history
 * entry would make the browser's back button undo a run, which it cannot.
 */
function setRunParam(runId: string | null) {
  const url = new URL(window.location.href)
  if (url.searchParams.get("run") === runId) return
  if (runId === null) url.searchParams.delete("run")
  else url.searchParams.set("run", runId)
  window.history.replaceState(null, "", url)
}
```
(`searchParams.get`은 값이 없으면 `null`을 돌려준다. 그래서 `null`을 넘겼는데 파라미터가 이미 없으면 아무것도 하지 않는다. 기존 `has` 검사와 같은 동작이다.)

- [ ] **Step 4: `inputSchema` 한 번만 계산**

`return (` 바로 위에 추가한다.
```ts
  const runInputs = inputSchema(state.dsl)
```
JSX의 다음 부분을
```tsx
      {inputSchema(state.dsl) === null ? null : (
        <RunDialog
          schema={inputSchema(state.dsl) ?? {}}
```
이렇게 바꾼다.
```tsx
      {runInputs === null ? null : (
        <RunDialog
          schema={runInputs}
```

- [ ] **Step 5: 지운 이름이 남지 않았는지 확인**

Run (`apps/web`에서):
```bash
grep -rn "useSaveStore\|useValidationStore\|useRunStore\|useRunInUrl\|clearRunFromUrl" components app lib store e2e
```
Expected: 출력 없음.

- [ ] **Step 6: 검증**

Run (`apps/web`에서):
```bash
pnpm test && pnpm typecheck && pnpm lint && pnpm e2e --project=component
```
Expected: 모두 통과하고 vitest 테스트 수는 기준선 그대로. e2e는 Task 0 Step 2에서 실행할 수 있었을 때만 돌린다.

- [ ] **Step 7: 커밋**

```bash
git add apps/web/components/canvas/Canvas.tsx
git commit -m "refactor(web): trim Canvas's one-caller wrappers and duplicated selection merge" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: engine의 미사용 `jsonschema` 제거

**Files:**
- Modify: `services/engine/pyproject.toml:9`, `services/engine/uv.lock`

- [ ] **Step 1: 미사용 재확인**

Run (`services/engine`에서):
```bash
grep -rn "jsonschema" engine tests README.md
```
Expected: 출력 없음. 하나라도 나오면 이 태스크를 멈추고 보고한다.

- [ ] **Step 2: 의존성 제거**

`pyproject.toml`에서 다음 줄을 지운다.
```toml
    "jsonschema>=4.26,<5",
```
그 다음 lock을 갱신한다.
```bash
uv lock
```
Expected: `Removed jsonschema ...` 또는 (다른 패키지가 간접 의존으로 쓰면) lock에는 남고 루트 의존성에서만 빠진다. 둘 다 정상이다.

- [ ] **Step 3: 검증**

Run (`services/engine`에서):
```bash
uv run pytest -q
```
Expected: 요약 줄이 Task 0 Step 3 기준선과 같다.

- [ ] **Step 4: 커밋**

```bash
git add services/engine/pyproject.toml services/engine/uv.lock
git commit -m "chore(engine): drop the unused jsonschema dependency" -m "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: 마무리 확인

- [ ] **Step 1: 순감소 확인**

Run (저장소 루트에서):
```bash
git diff --stat 687e7f1..HEAD -- apps services
```
Expected: 삭제가 추가보다 대략 60줄 많다. 오히려 늘었다면 원인을 보고한다.

- [ ] **Step 2: 통합은 superpowers:finishing-a-development-branch 로 진행** (PR 생성 여부는 사용자에게 묻는다)
