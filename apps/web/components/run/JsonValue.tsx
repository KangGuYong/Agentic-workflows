"use client"

import { isRedacted, scrubRedactions } from "@/lib/run/trace"

/** A recorded value, rendered so a redaction cannot be mistaken for text (3 설계 §8.5).
 *
 * The engine replaces secrets with the literal string `[REDACTED]` before storing them. Rendering the
 * record with `JSON.stringify` would print that string, which reads like the secret is *in* the record
 * -- and someone glancing at it cannot tell a redacted field from a field whose value happens to be
 * those ten characters. So the value is walked and every marker becomes a badge.
 *
 * A tenant's own literal `[REDACTED]` is shown as a badge too. That is the safe way round: a badge
 * where text belonged is cosmetic, text where a badge belonged looks like a leak.
 */

export function JsonValue({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (isRedacted(value)) return <Redaction />

  if (value === null) return <span className="text-fg-faint">null</span>
  if (typeof value === "boolean") return <span className="text-fg-muted">{value ? "true" : "false"}</span>
  if (typeof value === "number") return <span className="text-fg-muted">{value}</span>
  if (typeof value === "string") return <span className="whitespace-pre-wrap break-words">{value}</span>

  // Past a few levels the indentation is wider than the panel and nobody is reading it as a tree
  // anyway. The JSON is still there, just not laid out -- **scrubbed first**, because `JSON.stringify`
  // would otherwise print the marker verbatim and undo the whole point of this component below depth 4.
  if (depth >= 4) {
    return <span className="text-fg-faint">{JSON.stringify(scrubRedactions(value))}</span>
  }

  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-fg-faint">[]</span>
    return (
      <ul className="ml-3 border-l border-ink-600 pl-2">
        {value.map((item, index) => (
          <li key={index} className="flex gap-1">
            <span className="text-fg-faint">{index}</span>
            <JsonValue value={item} depth={depth + 1} />
          </li>
        ))}
      </ul>
    )
  }

  const entries = Object.entries(value as Record<string, unknown>)
  if (entries.length === 0) return <span className="text-fg-faint">{"{}"}</span>
  return (
    <ul className="ml-3 border-l border-ink-600 pl-2">
      {entries.map(([key, item]) => (
        <li key={key} className="flex flex-wrap gap-1">
          <span className="text-fg-faint">{key}</span>
          <JsonValue value={item} depth={depth + 1} />
        </li>
      ))}
    </ul>
  )
}

/** The badge that stands in for a secret. Carries a word, not only a lock: an icon alone is a guess. */
function Redaction() {
  return (
    <span
      className="inline-flex items-center gap-1 px-1.5 text-[0.6875rem] text-fg-faint"
      style={{ background: "var(--ink-700)", borderRadius: "var(--radius)" }}
      title="보안을 위해 값이 가려졌습니다"
    >
      <span aria-hidden>🔒</span>
      가려짐
    </span>
  )
}
