"use client"

import { useCallback, useEffect, useReducer, useRef } from "react"

import {
  applyEvent,
  emptyStream,
  markCancelling,
  markDisconnected,
  TERMINAL_EVENTS,
  type RunEvent,
  type StreamState,
} from "@/lib/run/events"

/** The run's event stream (3 설계 §8.1, Task 15).
 *
 * Everything this does beyond opening a socket lives in `lib/run/events.ts`, which is pure and tested.
 * What is here is the three things only a live `EventSource` has:
 *
 * 1. **One connection at a time.** Opening a second while one is live doubles every event, and the
 *    reducer would count each twice.
 * 2. **Closed explicitly on a terminal event.** `EventSource` reconnects on its own when the server
 *    closes a stream, so a run that ended would be reopened forever, each time replaying the whole run.
 * 3. **No reconnection logic of our own.** `EventSource` already backs off and resends `Last-Event-ID`.
 *    A second mechanism on top would fight it.
 */

/** Every event type the engine can send, since `EventSource` dispatches by name and ignores the rest. */
const EVENT_TYPES = [
  "run_queued",
  "run_started",
  "run_resumed",
  "run_recovered",
  "run_waiting",
  "run_cancel_requested",
  "run_succeeded",
  "run_failed",
  "run_cancelled",
  "node_started",
  "node_finished",
  "node_failed",
  "node_waiting",
  "node_token",
]

type Action =
  | { kind: "event"; event: RunEvent }
  | { kind: "disconnected" }
  | { kind: "cancelling" }
  | { kind: "reset" }

function reduce(state: StreamState, action: Action): StreamState {
  if (action.kind === "reset") return emptyStream()
  if (action.kind === "disconnected") return markDisconnected(state)
  if (action.kind === "cancelling") return markCancelling(state)
  return applyEvent(state, action.event)
}

export function useRunStream(runId: string | null): StreamState & { markCancelling: () => void } {
  const [state, dispatch] = useReducer(reduce, undefined, emptyStream)
  // Owned entirely by the effect below: the handlers need to know whether the run ended, and `state`
  // there is whatever it was when the connection opened. Writing it during render is both a React rule
  // violation and unnecessary -- the only transition to `true` is the terminal frame the handler sees.
  const finished = useRef(false)

  useEffect(() => {
    if (runId === null) return
    dispatch({ kind: "reset" })
    finished.current = false

    const source = new EventSource(`/api/engine/runs/${encodeURIComponent(runId)}/events`)

    function onFrame(message: MessageEvent<string>) {
      let event: RunEvent
      try {
        event = JSON.parse(message.data) as RunEvent
      } catch {
        // A frame that is not JSON is not something to act on, and it is certainly not a reason to
        // tear down a stream that is otherwise delivering.
        return
      }
      dispatch({ kind: "event", event })
      if (TERMINAL_EVENTS.has(event.type)) {
        // The server will close after this. Left alone, `EventSource` would reconnect and replay the
        // whole run, forever.
        finished.current = true
        source.close()
      }
    }

    for (const type of EVENT_TYPES) source.addEventListener(type, onFrame as EventListener)

    source.onerror = () => {
      // `EventSource` is already reconnecting; this only says so on screen. Closing the source here
      // would replace its backoff with nothing.
      if (finished.current) source.close()
      else dispatch({ kind: "disconnected" })
    }

    return () => source.close()
  }, [runId])

  const requestCancel = useCallback(() => dispatch({ kind: "cancelling" }), [])

  return { ...state, markCancelling: requestCancel }
}
