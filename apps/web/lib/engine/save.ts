import type { EditorDsl } from "@/lib/dsl/document"
import type { SaveResult } from "@/store/save"

/** `PUT /workflows/{id}` from the browser, through the BFF proxy (3 설계 §3.2, §9).
 *
 * Turns the engine's three answers into the three the save store understands. Everything that is not a
 * clean 200 or a well-formed 409 is a failure -- including a 409 whose `details` are not what the
 * contract says, because "conflict with an unknown other draft" is not something the dialog can offer
 * a choice about.
 */

interface ErrorEnvelope {
  error?: { code?: unknown; message?: unknown; details?: unknown }
}

function messageOf(body: unknown, fallback: string): string {
  const message = (body as ErrorEnvelope | null)?.error?.message
  return typeof message === "string" && message !== "" ? message : fallback
}

function conflictOf(body: unknown): { currentRevision: number; draftDsl: EditorDsl } | null {
  const details = (body as ErrorEnvelope | null)?.error?.details
  if (typeof details !== "object" || details === null) return null
  const { currentRevision, draftDsl } = details as Record<string, unknown>
  if (typeof currentRevision !== "number" || typeof draftDsl !== "object" || draftDsl === null) return null
  return { currentRevision, draftDsl: draftDsl as EditorDsl }
}

export async function saveDraft(
  workflowId: string,
  body: { draftDsl: EditorDsl; revision: number },
  init: { signal?: AbortSignal } = {},
): Promise<SaveResult> {
  const response = await fetch(`/api/engine/workflows/${encodeURIComponent(workflowId)}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal: init.signal,
  })

  // `json()` throws on an empty or non-JSON body, which a proxy error page is.
  const parsed: unknown = await response.json().catch(() => null)

  if (response.ok) {
    const revision = (parsed as { revision?: unknown } | null)?.revision
    if (typeof revision !== "number") {
      return { outcome: "failed", message: "저장 응답을 이해하지 못했습니다" }
    }
    return { outcome: "saved", revision }
  }

  if (response.status === 409) {
    const conflict = conflictOf(parsed)
    if (conflict !== null) return { outcome: "conflict", ...conflict }
    // A 409 the dialog cannot act on. Saying "conflict" without the other draft would offer a choice
    // between this document and nothing.
    return { outcome: "failed", message: messageOf(parsed, "다른 곳에서 먼저 저장했습니다") }
  }

  return { outcome: "failed", message: messageOf(parsed, `저장하지 못했습니다 (${response.status})`) }
}
