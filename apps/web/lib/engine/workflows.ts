/** Workflow list operations from the browser, through the BFF proxy (Task 19). */

export interface WorkflowSummary {
  id: string
  name: string
  revision: number
  updatedAt?: string
}

export type ListResult =
  | { outcome: "ok"; workflows: WorkflowSummary[] }
  | { outcome: "failed"; message: string }

export type MutationResult<T = void> =
  | ({ outcome: "ok" } & (T extends void ? Record<string, never> : { value: T }))
  | { outcome: "blocked"; message: string }
  | { outcome: "failed"; message: string }

interface Envelope {
  error?: { code?: unknown; message?: unknown }
}

function messageOf(body: unknown, fallback: string): string {
  const message = (body as Envelope | null)?.error?.message
  return typeof message === "string" && message !== "" ? message : fallback
}

function summaryOf(value: unknown): WorkflowSummary | null {
  if (typeof value !== "object" || value === null) return null
  const { id, name, revision, updatedAt } = value as Record<string, unknown>
  if (typeof id !== "string" || typeof name !== "string" || typeof revision !== "number") return null
  return { id, name, revision, updatedAt: typeof updatedAt === "string" ? updatedAt : undefined }
}

export async function listWorkflows(init: { signal?: AbortSignal } = {}): Promise<ListResult> {
  const response = await fetch("/api/engine/workflows", { signal: init.signal })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    return { outcome: "failed", message: messageOf(body, `목록을 불러오지 못했습니다 (${response.status})`) }
  }
  const raw = (body as { workflows?: unknown } | null)?.workflows
  if (!Array.isArray(raw)) return { outcome: "failed", message: "목록 응답을 이해하지 못했습니다" }
  // A row the editor cannot read is dropped rather than rendered half-blank; the rest of the list is
  // still usable, which is not true if one malformed row throws.
  return { outcome: "ok", workflows: raw.map(summaryOf).filter((row): row is WorkflowSummary => row !== null) }
}

export async function createWorkflow(name: string): Promise<MutationResult<WorkflowSummary>> {
  const response = await fetch("/api/engine/workflows", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name }),
  })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    return { outcome: "failed", message: messageOf(body, `워크플로를 만들지 못했습니다 (${response.status})`) }
  }
  const created = summaryOf(body)
  return created === null
    ? { outcome: "failed", message: "생성 응답을 이해하지 못했습니다" }
    : { outcome: "ok", value: created }
}

/** Replace a workflow's name **and** draft in one write.
 *
 * `PUT` replaces the whole row, so both always travel together: renaming sends the draft back
 * unchanged, and importing sends the stored name back unchanged. Naming it for one of those two uses
 * would make the other read like a mistake.
 */
export async function putWorkflow(
  id: string,
  name: string,
  draftDsl: unknown,
  revision: number,
): Promise<MutationResult<number>> {
  const response = await fetch(`/api/engine/workflows/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name, draftDsl, revision }),
  })
  const body: unknown = await response.json().catch(() => null)
  if (response.status === 409) {
    return { outcome: "blocked", message: messageOf(body, "다른 곳에서 먼저 저장했습니다") }
  }
  if (!response.ok) {
    return { outcome: "failed", message: messageOf(body, `저장하지 못했습니다 (${response.status})`) }
  }
  const next = (body as { revision?: unknown } | null)?.revision
  return typeof next === "number"
    ? { outcome: "ok", value: next }
    : { outcome: "failed", message: "저장 응답을 이해하지 못했습니다" }
}

/** Read a workflow's stored draft. Used by rename (which must send it back) and by export. */
export async function openDraft(id: string): Promise<MutationResult<{ draftDsl: unknown; revision: number }>> {
  const response = await fetch(`/api/engine/workflows/${encodeURIComponent(id)}`)
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    return { outcome: "failed", message: messageOf(body, `워크플로를 불러오지 못했습니다 (${response.status})`) }
  }
  const { draftDsl, revision } = (body ?? {}) as { draftDsl?: unknown; revision?: unknown }
  return typeof revision === "number"
    ? { outcome: "ok", value: { draftDsl, revision } }
    : { outcome: "failed", message: "워크플로 응답을 이해하지 못했습니다" }
}

export async function deleteWorkflow(id: string): Promise<MutationResult> {
  const response = await fetch(`/api/engine/workflows/${encodeURIComponent(id)}`, { method: "DELETE" })
  if (response.status === 204) return { outcome: "ok" } as MutationResult
  const body: unknown = await response.json().catch(() => null)
  if (response.status === 409) {
    // `WORKFLOW_HAS_ACTIVE_RUNS`. Not a failure to retry: it is a refusal with a reason, and the reason
    // is something the tenant can act on -- wait, or cancel the run.
    return { outcome: "blocked", message: messageOf(body, "실행 중인 워크플로는 삭제할 수 없습니다") }
  }
  return { outcome: "failed", message: messageOf(body, `삭제하지 못했습니다 (${response.status})`) }
}
