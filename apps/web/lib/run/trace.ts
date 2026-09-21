/** Reading a run's node history (3 설계 §8.5, Task 16).
 *
 * `GET /runs/{id}/nodes` returns one row per **attempt**, not per node: a node that was retried three
 * times has three rows, and a node inside a loop has one per iteration. The panel shows them as they
 * are rather than collapsing them, because "it worked" and "it worked on the third try" are different
 * facts about a workflow.
 */

export const REDACTED = "[REDACTED]"

export type NodeRunStatus = "running" | "succeeded" | "defaulted" | "failed" | "waiting"

/** One row of `GET /runs/{id}/nodes` (engine `routers/runs.py::_node_run_view`). */
export interface NodeRun {
  nodeId: string
  execIndex: number
  attempt: number
  status: NodeRunStatus
  input: unknown
  output: unknown
  error: { code?: string; message?: string } | null
  meta: unknown
  /** The engine kept only part of a value that was too large to store whole. */
  truncated: boolean
  tokensIn: number
  tokensOut: number
  startedAt: string
  finishedAt: string | null
}

export interface NodeRunPage {
  nodeRuns: NodeRun[]
  hasMore: boolean
}

/** The rows belonging to one node, newest attempt first.
 *
 * Sorted here rather than trusted from the API: the panel's ordering is a display decision, and the
 * most recent attempt is the one someone is looking for.
 */
export function runsForNode(runs: readonly NodeRun[], nodeId: string): NodeRun[] {
  return runs
    .filter((run) => run.nodeId === nodeId)
    .sort((a, b) => b.execIndex - a.execIndex || b.attempt - a.attempt)
}

/** How long an attempt took, in milliseconds, or null while it is still going. */
export function durationMs(run: NodeRun): number | null {
  if (run.finishedAt === null) return null
  const started = Date.parse(run.startedAt)
  const finished = Date.parse(run.finishedAt)
  if (!Number.isFinite(started) || !Number.isFinite(finished)) return null
  // Clamped at zero: the two timestamps come from the same clock but nothing guarantees monotonicity
  // across a process restart, and a negative duration is not a thing to show anyone.
  return Math.max(0, finished - started)
}

/** A duration a person reads, not a number of milliseconds. */
export function durationText(ms: number | null): string {
  if (ms === null) return "진행 중"
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}초`
  const minutes = Math.floor(ms / 60_000)
  const seconds = Math.round((ms % 60_000) / 1000)
  return seconds === 0 ? `${minutes}분` : `${minutes}분 ${seconds}초`
}

/** Whether a value is the engine's redaction marker rather than a real string.
 *
 * The marker is a *string equal to* `[REDACTED]`, which is also a string a tenant could legitimately
 * have in their data. Both render as the badge: showing a tenant's literal `[REDACTED]` as a lock is a
 * cosmetic mistake, while showing a real redaction as plain text looks like the secret leaked.
 */
export function isRedacted(value: unknown): boolean {
  return value === REDACTED
}

/** Whether an attempt carries anything worth expanding. */
export function hasDetail(run: NodeRun): boolean {
  return run.input !== null || run.output !== null || run.error !== null
}

/** The marker as it reads inside a compact JSON dump, where a badge cannot be drawn. */
export const REDACTED_TEXT = "🔒 가려짐"

/** `value` with every redaction marker replaced by something that cannot be mistaken for the secret.
 *
 * Needed wherever a value is printed rather than walked -- below the depth at which the tree stops
 * expanding, `JSON.stringify` would otherwise put `[REDACTED]` straight back on screen, which is the
 * one thing the badge exists to prevent.
 */
export function scrubRedactions(value: unknown): unknown {
  if (isRedacted(value)) return REDACTED_TEXT
  if (Array.isArray(value)) return value.map(scrubRedactions)
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, item]) => [key, scrubRedactions(item)]),
    )
  }
  return value
}
