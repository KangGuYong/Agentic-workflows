/** What a run's event stream means (3 설계 §8.1, Task 15).
 *
 * A pure reducer, deliberately. Everything interesting about a stream is *ordering* -- a token arriving
 * after a node finished, a `node_failed` that will be retried, a terminal event landing twice -- and none
 * of it needs a socket to test. The `EventSource` in `useRunStream` does nothing but hand frames to this.
 *
 * Event shapes are the engine's (`engine/runtime/recorder.py`, `engine/worker/worker.py`).
 */

export type NodeStatus = "running" | "succeeded" | "defaulted" | "failed" | "waiting"

/** `engine/events/stream.py::TERMINAL`. Nothing follows one of these. */
export const TERMINAL_EVENTS = new Set(["run_succeeded", "run_failed", "run_cancelled"])

export type RunStatus = "queued" | "running" | "waiting" | "succeeded" | "failed" | "cancelled"

export interface RunEvent {
  type: string
  seq?: number
  nodeId?: string
  execIndex?: number
  attempt?: number | null
  text?: string
  error?: { code?: string; message?: string }
  willRetry?: boolean
  defaulted?: boolean
  payload?: unknown
  [key: string]: unknown
}

export interface NodeRunState {
  status: NodeStatus
  attempt: number | null
  /** Streamed text, for the token preview. Only ever appended to. */
  tokens: string
  error: { code?: string; message?: string } | null
  /** True when the node finished on its `defaultOutput` rather than on its own result. */
  defaulted: boolean
}

export interface StreamState {
  status: RunStatus
  nodes: Record<string, NodeRunState>
  /** The last `id:` the engine sent, for a reconnect to resume after (transient events carry none). */
  lastSeq: number | null
  /** True once a terminal event arrived: the stream is over and must not be reopened. */
  finished: boolean
  /** Set by a transport error while the run is still going; cleared by the next message. */
  disconnected: boolean
}

export function emptyStream(): StreamState {
  return { status: "queued", nodes: {}, lastSeq: null, finished: false, disconnected: false }
}

const RUN_STATUS: Record<string, RunStatus> = {
  run_queued: "queued",
  run_started: "running",
  run_resumed: "running",
  run_recovered: "running",
  run_waiting: "waiting",
  run_succeeded: "succeeded",
  run_failed: "failed",
  run_cancelled: "cancelled",
}

function nodeOf(state: StreamState, nodeId: string): NodeRunState {
  return state.nodes[nodeId] ?? { status: "running", attempt: null, tokens: "", error: null, defaulted: false }
}

function withNode(state: StreamState, nodeId: string, change: Partial<NodeRunState>): StreamState {
  return { ...state, nodes: { ...state.nodes, [nodeId]: { ...nodeOf(state, nodeId), ...change } } }
}

export function applyEvent(state: StreamState, event: RunEvent): StreamState {
  // Anything after a terminal event is not ours: a replayed frame, or a stream that should have closed.
  // Acting on it would move a finished run back to running.
  if (state.finished) return state

  const seq = typeof event.seq === "number" ? event.seq : null
  const next: StreamState = {
    ...state,
    // Transient events (node_token) carry no id, so they must not move the resume point backwards.
    lastSeq: seq === null ? state.lastSeq : Math.max(seq, state.lastSeq ?? 0),
    // Any message at all means the connection is alive again.
    disconnected: false,
  }

  const runStatus = RUN_STATUS[event.type]
  if (runStatus !== undefined) {
    return { ...next, status: runStatus, finished: TERMINAL_EVENTS.has(event.type) }
  }

  const nodeId = event.nodeId
  if (typeof nodeId !== "string") return next

  const attempt = typeof event.attempt === "number" ? event.attempt : null

  switch (event.type) {
    case "node_started":
      // A retry starts a fresh attempt: the previous attempt's tokens and error belong to that attempt,
      // not to this one, and leaving them would show the old failure under a node that is running again.
      return withNode(next, nodeId, { status: "running", attempt, tokens: "", error: null, defaulted: false })
    case "node_finished":
      return withNode(next, nodeId, {
        status: event.defaulted === true ? "defaulted" : "succeeded",
        attempt,
        defaulted: event.defaulted === true,
      })
    case "node_failed":
      // `willRetry` means the engine is about to try again. Showing "failed" then would be a lie the
      // next `node_started` immediately corrects -- a flicker that reads as a bug.
      return withNode(next, nodeId, {
        status: event.willRetry === true ? "running" : "failed",
        attempt,
        error: event.error ?? null,
      })
    case "node_waiting":
      return withNode(next, nodeId, { status: "waiting", attempt })
    case "node_token":
      // Appends, and **never** changes the status. A token is output, not a state transition; letting
      // it set "running" would resurrect a node that had already finished.
      return withNode(next, nodeId, { tokens: nodeOf(next, nodeId).tokens + (event.text ?? "") })
    default:
      // An event type this editor does not know is not an error. A newer engine may send one, and the
      // right response is to keep the connection and ignore the frame.
      return next
  }
}

/** Mark the stream as disconnected, without touching anything the run reported. */
export function markDisconnected(state: StreamState): StreamState {
  return state.finished ? state : { ...state, disconnected: true }
}
