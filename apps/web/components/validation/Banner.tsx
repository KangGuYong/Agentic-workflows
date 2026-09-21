"use client"

import { koreanMessage } from "@/lib/errors/korean"
import type { Issue } from "@/store/validation"

/** Problems that belong to the workflow rather than to any one node (Task 13).
 *
 * `LIMIT_EXCEEDED` is the case this exists for: the document is too big, which is not a fact about any
 * node. Putting that on a node's badge would send someone to fix a node that is fine.
 */
export function Banner({ issues }: { issues: readonly Issue[] }) {
  if (issues.length === 0) return null

  return (
    <div
      role="alert"
      className="pointer-events-auto border px-3 py-2 text-xs"
      style={{
        borderRadius: "var(--radius)",
        borderColor: "var(--st-failed)",
        background: "color-mix(in srgb, var(--st-failed) 12%, var(--ink-800))",
      }}
    >
      <ul className="flex flex-col gap-1">
        {issues.map((issue, index) => (
          <li key={`${issue.code}-${index}`}>{koreanMessage(issue.message)}</li>
        ))}
      </ul>
    </div>
  )
}
