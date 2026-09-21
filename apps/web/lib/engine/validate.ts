import type { EditorDsl } from "@/lib/dsl/document"
import type { ValidationResponse } from "@/store/validation"

/** `POST /workflows/{id}/validate` from the browser, through the BFF proxy (3 설계 §4.1).
 *
 * The engine always answers 200 with an issue list -- including for a draft it cannot parse -- so a
 * non-200 here is a transport problem, not a verdict about the workflow. It throws, and the slice keeps
 * the issues it had rather than claiming the workflow is clean.
 */
export async function validateDraft(
  workflowId: string,
  draftDsl: EditorDsl,
  init: { signal?: AbortSignal } = {},
): Promise<ValidationResponse> {
  const response = await fetch(
    `/api/engine/workflows/${encodeURIComponent(workflowId)}/validate`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ draftDsl }),
      signal: init.signal,
    },
  )
  if (!response.ok) throw new Error(`검증 요청 실패 (${response.status})`)

  const body: unknown = await response.json()
  const issues = (body as { issues?: unknown } | null)?.issues
  if (!Array.isArray(issues)) throw new Error("검증 응답을 이해하지 못했습니다")

  const nodes = (body as { nodes?: unknown }).nodes
  return {
    issues: issues as ValidationResponse["issues"],
    nodes: typeof nodes === "object" && nodes !== null ? (nodes as ValidationResponse["nodes"]) : undefined,
  }
}
