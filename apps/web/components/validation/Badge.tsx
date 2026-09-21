"use client"

import { koreanMessage } from "@/lib/errors/korean"
import type { Issue, Severity } from "@/store/validation"

/** The badge a node or edge wears when `/validate` has something to say about it (Task 13).
 *
 * Colour is not the only signal: a glyph carries the same information, because a red dot and a yellow
 * dot are the same dot to a colour-blind reader. The count is on the badge because "three problems
 * here" and "one" call for different amounts of attention before clicking.
 */

const GLYPH: Record<Severity, string> = { error: "!", warning: "?" }
const COLOUR: Record<Severity, string> = { error: "var(--st-failed)", warning: "var(--st-waiting)" }

export function Badge({ issues, severity }: { issues: readonly Issue[]; severity: Severity }) {
  // The first message is the tooltip; the rest are counted. Concatenating five sentences into a
  // `title` produces something nobody reads.
  const first = issues[0]
  const label =
    issues.length === 1
      ? koreanMessage(first?.message ?? "")
      : `${koreanMessage(first?.message ?? "")} 외 ${issues.length - 1}건`

  return (
    <span
      role="img"
      aria-label={`${severity === "error" ? "오류" : "경고"}: ${label}`}
      title={label}
      className="flex size-4 items-center justify-center text-[10px] font-bold text-ink-800"
      style={{ background: COLOUR[severity], borderRadius: "9999px" }}
    >
      <span aria-hidden>{issues.length > 1 ? issues.length : GLYPH[severity]}</span>
    </span>
  )
}

/** The message list shown inside the panel, under the field it belongs to. */
export function IssueList({ issues }: { issues: readonly Issue[] }) {
  if (issues.length === 0) return null
  return (
    <ul className="mt-1 flex flex-col gap-0.5">
      {issues.map((issue, index) => (
        <li
          key={`${issue.code}-${index}`}
          className="text-xs"
          style={{ color: issue.severity === "error" ? "var(--st-failed)" : "var(--st-waiting)" }}
        >
          {koreanMessage(issue.message)}
        </li>
      ))}
    </ul>
  )
}
