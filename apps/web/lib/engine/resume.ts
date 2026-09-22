/** Answering an approval and cancelling a run (3 설계 §8.4, Task 17). */

import { messageOf } from "./envelope"

export type Decision = "approve" | "reject"

export interface ResumeBody {
  nodeId: string
  execIndex: number
  decision: Decision
  comment?: string
  editedValue?: unknown
}

export type ResumeResult =
  | { outcome: "queued" }
  /** Someone else answered first, or the run moved on. */
  | { outcome: "stale"; message: string }
  | { outcome: "rejected"; message: string }
  | { outcome: "failed"; message: string }

/** Every key the engine accepts in an answer (`human_approval._ANSWER_KEYS`) **except `reviewedAt`**.
 *
 * `reviewedAt` is the server's clock, always: `resume_run` discards a client-supplied value before
 * validation ever sees it, because a node has no way to tell a forged timestamp from a real one.
 * Sending one anyway would be harmless and misleading -- it would suggest this browser's clock decides
 * when a review happened, and a reader of this code would have to go and find out that it does not.
 */
const ANSWER_KEYS = ["nodeId", "execIndex", "decision", "comment", "editedValue"] as const

function answerFrom(body: ResumeBody): Record<string, unknown> {
  const answer: Record<string, unknown> = {}
  for (const key of ANSWER_KEYS) {
    const value = body[key]
    // The engine rejects an answer with an unknown *or* undefined-shaped key, and `comment` and
    // `editedValue` are genuinely optional, so an absent one is left out rather than sent as null.
    if (value !== undefined) answer[key] = value
  }
  return answer
}

export async function resumeRun(
  runId: string,
  body: ResumeBody,
  init: { signal?: AbortSignal } = {},
): Promise<ResumeResult> {
  const response = await fetch(`/api/engine/runs/${encodeURIComponent(runId)}/resume`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(answerFrom(body)),
    signal: init.signal,
  })

  if (response.ok) return { outcome: "queued" }
  const parsed: unknown = await response.json().catch(() => null)

  if (response.status === 409) {
    // All three 409s mean the same thing to a reviewer: this approval is no longer theirs to answer.
    return { outcome: "stale", message: messageOf(parsed, "이미 처리된 승인입니다") }
  }
  if (response.status === 422) {
    // Already Korean: `resume_output` writes its own messages.
    return { outcome: "rejected", message: messageOf(parsed, "승인 응답을 처리할 수 없습니다") }
  }
  return {
    outcome: "failed",
    message: messageOf(parsed, `승인을 보내지 못했습니다 (${response.status})`),
  }
}

export type CancelResult =
  /** The run was queued or parked, so the API ended it outright. */
  | { outcome: "cancelled" }
  /** A worker holds it; it stops at its next heartbeat. The UI waits for `run_cancelled`. */
  | { outcome: "requested" }
  | { outcome: "failed"; message: string }

export async function cancelRun(runId: string, init: { signal?: AbortSignal } = {}): Promise<CancelResult> {
  const response = await fetch(`/api/engine/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
    signal: init.signal,
  })
  const parsed: unknown = await response.json().catch(() => null)

  if (response.ok) {
    // `{"status": "cancelled"}` means done; `{"status": "running"}` means the worker was told.
    return (parsed as { status?: unknown } | null)?.status === "cancelled"
      ? { outcome: "cancelled" }
      : { outcome: "requested" }
  }
  return { outcome: "failed", message: messageOf(parsed, `실행을 취소하지 못했습니다 (${response.status})`) }
}
