import type { EditorDsl } from "@/lib/dsl/document"
import type { Issue } from "@/store/validation"

import { detailsOf, messageOf } from "./envelope"

/** Starting a run (3 설계 §8, Task 14).
 *
 * `POST /workflows/{id}/runs` answers four ways that mean different things to the editor, so the result
 * is a union rather than a thrown error with a string in it.
 *
 * The **idempotency key is fresh per submission**, never per workflow or per session. The engine dedupes
 * by it (`run_db.find_by_idempotency_key`), which is what makes a retried request safe -- and what would
 * make two deliberate runs collapse into one if the key were reused. A double click should start one
 * run; pressing 실행 twice on purpose should start two.
 */

export interface RunStarted {
  outcome: "started"
  runId: string
  versionId: string
}

export interface RunStale {
  outcome: "stale"
  currentRevision: number
  /** Fetched separately: the run 409 carries only the revision, not the other draft. */
  draftDsl: EditorDsl | null
}

export interface RunRejected {
  outcome: "rejected"
  issues: Issue[]
}

export interface RunFailed {
  outcome: "failed"
  message: string
}

export type RunResult = RunStarted | RunStale | RunRejected | RunFailed

/** The current draft, for a conflict the run endpoint describes only by revision. */
async function currentDraft(workflowId: string): Promise<EditorDsl | null> {
  try {
    const response = await fetch(`/api/engine/workflows/${encodeURIComponent(workflowId)}`)
    if (!response.ok) return null
    const body: unknown = await response.json()
    const draft = (body as { draftDsl?: unknown } | null)?.draftDsl
    return typeof draft === "object" && draft !== null ? (draft as EditorDsl) : null
  } catch {
    // Without it the dialog can still offer 덮어쓰기; it just cannot offer 불러오기.
    return null
  }
}

export async function startRun(
  workflowId: string,
  body: { inputs: Record<string, unknown>; revision: number },
  init: { signal?: AbortSignal; idempotencyKey?: string } = {},
): Promise<RunResult> {
  const response = await fetch(`/api/engine/workflows/${encodeURIComponent(workflowId)}/runs`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      // One per submission. `crypto.randomUUID` is available in every browser this app supports and in
      // Node 19+, so there is no fallback to a weaker id.
      "idempotency-key": init.idempotencyKey ?? crypto.randomUUID(),
    },
    body: JSON.stringify(body),
    signal: init.signal,
  })

  const parsed: unknown = await response.json().catch(() => null)

  if (response.ok) {
    const { runId, versionId } = (parsed ?? {}) as { runId?: unknown; versionId?: unknown }
    if (typeof runId !== "string" || typeof versionId !== "string") {
      return { outcome: "failed", message: "실행 응답을 이해하지 못했습니다" }
    }
    return { outcome: "started", runId, versionId }
  }

  if (response.status === 409) {
    const revision = detailsOf(parsed)?.["currentRevision"]
    if (typeof revision !== "number") {
      return { outcome: "failed", message: messageOf(parsed, "다른 곳에서 먼저 저장했습니다") }
    }
    // The run endpoint's 409 names the revision and nothing else, unlike the save endpoint's. The
    // conflict dialog offers a choice between two drafts, so the other one has to be fetched.
    return { outcome: "stale", currentRevision: revision, draftDsl: await currentDraft(workflowId) }
  }

  if (response.status === 422) {
    const issues = detailsOf(parsed)?.["issues"]
    if (Array.isArray(issues)) return { outcome: "rejected", issues: issues as Issue[] }
    return { outcome: "failed", message: messageOf(parsed, "워크플로에 오류가 있습니다") }
  }

  return { outcome: "failed", message: messageOf(parsed, `실행을 시작하지 못했습니다 (${response.status})`) }
}
