"use client"

import { errorCount, type Issue } from "@/store/validation"

/** The 실행 button, and the rule that stops it (Task 13).
 *
 * Task 14 gives it behaviour. It exists now because what blocks a run is a **validation** question: the
 * engine refuses a workflow with errors, so letting someone press 실행 only to be refused wastes the
 * press and teaches them the button is unreliable.
 *
 * The tooltip counts rather than lists. "오류 3건" sends someone looking at the three badges already on
 * the canvas; three sentences in a tooltip do not fit and are not where the fixing happens.
 */
export function runBlockedReason(issues: readonly Issue[], nodeCount: number): string | null {
  if (nodeCount === 0) return "노드를 먼저 놓아 주세요"
  const errors = errorCount(issues)
  return errors > 0 ? `오류 ${errors}건을 먼저 해결해 주세요` : null
}

export function RunButton({
  issues,
  nodeCount,
  onRun,
}: {
  issues: readonly Issue[]
  nodeCount: number
  onRun?: () => void
}) {
  const blocked = runBlockedReason(issues, nodeCount)

  return (
    <button
      type="button"
      onClick={onRun}
      disabled={blocked !== null}
      title={blocked ?? undefined}
      // The reason is on the button itself, not only in a `title`: a tooltip does not exist on a touch
      // screen and is not read by every screen reader.
      aria-describedby={blocked === null ? undefined : "run-blocked"}
      className="pointer-events-auto border border-ink-600 bg-ink-700 px-3 py-1.5 text-xs disabled:opacity-35"
      style={{ borderRadius: "var(--radius)" }}
    >
      실행
      {blocked === null ? null : (
        <span id="run-blocked" className="sr-only">
          {blocked}
        </span>
      )}
    </button>
  )
}
