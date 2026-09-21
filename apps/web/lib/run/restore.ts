import { emptyStream, TERMINAL_EVENTS, type RunStatus, type StreamState, type WaitingFor } from "./events"
import type { NodeRun } from "./trace"

/** Rebuilding a run's state after a reload (3 설계 §8.3, Task 18).
 *
 * `?run=` survives a refresh; the stream does not. So the editor reads the run and its node rows
 * **before** opening a stream, paints what it learned, and only then connects -- otherwise the first
 * frame to arrive would be painted onto an empty canvas that says the run never happened.
 *
 * A finished run needs no stream at all. Opening one would replay every stored event to arrive at the
 * state already on screen, and `EventSource` would then reconnect to a stream the server closes at
 * once, forever.
 */

/** `GET /runs/{id}` (engine `routers/runs.py::_run_view`). */
export interface RunView {
  id: string
  status: string
  cancelRequested: boolean
  waitingFor?: unknown
  error?: unknown
}

const RUN_STATUS: Record<string, RunStatus> = {
  queued: "queued",
  running: "running",
  waiting: "waiting",
  succeeded: "succeeded",
  failed: "failed",
  cancelled: "cancelled",
}

const TERMINAL_STATUS = new Set<RunStatus>(["succeeded", "failed", "cancelled"])

function waitingOf(value: unknown): WaitingFor | null {
  if (typeof value !== "object" || value === null) return null
  const { nodeId, execIndex, message, review, allowEdit } = value as Record<string, unknown>
  if (typeof nodeId !== "string" || typeof execIndex !== "number") return null
  return {
    nodeId,
    execIndex,
    message: typeof message === "string" ? message : "",
    review,
    allowEdit: allowEdit === true,
  }
}

/** The state a reload should start from.
 *
 * Node state comes from the **latest attempt** of each node: the rows are per attempt, and what a
 * canvas shows is where each node stands now, not every step it took to get there. The trace panel is
 * where the attempts are read one by one.
 */
export function restoreStream(run: RunView, nodeRuns: readonly NodeRun[]): StreamState {
  const status = RUN_STATUS[run.status] ?? "queued"
  const finished = TERMINAL_STATUS.has(status)

  const latest = new Map<string, NodeRun>()
  for (const nodeRun of nodeRuns) {
    const current = latest.get(nodeRun.nodeId)
    const newer =
      current === undefined ||
      nodeRun.execIndex > current.execIndex ||
      (nodeRun.execIndex === current.execIndex && nodeRun.attempt > current.attempt)
    if (newer) latest.set(nodeRun.nodeId, nodeRun)
  }

  const nodes: StreamState["nodes"] = {}
  for (const [nodeId, nodeRun] of latest) {
    nodes[nodeId] = {
      status: nodeRun.status,
      attempt: nodeRun.attempt,
      // Tokens are transient: the engine gives them no `seq` and never stores them (설계 7.3), so a
      // reload cannot get back what was streamed. Claiming otherwise would mean inventing text.
      tokens: "",
      error: nodeRun.error,
      defaulted: nodeRun.status === "defaulted",
    }
  }

  return {
    ...emptyStream(),
    status,
    nodes,
    finished,
    waitingFor: waitingOf(run.waitingFor),
    // A cancel that was asked for and has not landed yet. A finished run has nothing left to cancel.
    cancelling: run.cancelRequested && !finished,
  }
}

/** Whether a stream should be opened for this run at all. */
export function shouldStream(state: StreamState): boolean {
  return !state.finished
}

/** The event types that end a run, for callers that only have the status string. */
export function isTerminalStatus(status: string): boolean {
  return TERMINAL_STATUS.has(RUN_STATUS[status] ?? "queued")
}

export { TERMINAL_EVENTS }
