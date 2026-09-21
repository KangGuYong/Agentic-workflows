import { createStore, type StoreApi } from "zustand/vanilla"

import type { EditorDsl } from "@/lib/dsl/document"
import type { RunResult } from "@/lib/engine/run"
import type { Issue } from "@/store/validation"

/** Starting a run, and what came of it (3 설계 §8, Task 14).
 *
 * Task 15 adds the event stream on top of `runId`. What lives here is only the submission: whether one
 * is in the air, which run it produced, and which of the engine's four answers came back.
 */

export type StartRequest = (body: {
  inputs: Record<string, unknown>
  revision: number
}) => Promise<RunResult>

export interface RunState {
  /** The run the URL should carry, or null when none has started. */
  runId: string | null
  starting: boolean
  /** A failure in the engine's own words, cleared by the next attempt. */
  error: string | null
  /** Issues from a 422: validation the editor thought was clean and the engine did not. */
  rejected: Issue[]
  /** The draft moved under us; the conflict dialog decides what happens next. */
  stale: { currentRevision: number; draftDsl: EditorDsl | null } | null

  start: (inputs: Record<string, unknown>, revision: number) => Promise<void>
  clearStale: () => void
}

export type RunStore = StoreApi<RunState>

export function createRunStore(request: StartRequest): RunStore {
  return createStore<RunState>((set, get) => ({
    runId: null,
    starting: false,
    error: null,
    rejected: [],
    stale: null,

    async start(inputs, revision) {
      // One submission at a time. A second press while the first is in the air would send a second
      // idempotency key and start a second run -- which is right for a deliberate second run, and wrong
      // for an impatient double click on a slow network.
      if (get().starting) return
      set({ starting: true, error: null, rejected: [], stale: null })

      let result: RunResult
      try {
        result = await request({ inputs, revision })
      } catch (error) {
        result = {
          outcome: "failed",
          message: error instanceof Error ? error.message : "실행을 시작하지 못했습니다",
        }
      }

      if (result.outcome === "started") set({ runId: result.runId, starting: false })
      else if (result.outcome === "rejected") set({ rejected: result.issues, starting: false })
      else if (result.outcome === "stale") {
        set({ stale: { currentRevision: result.currentRevision, draftDsl: result.draftDsl }, starting: false })
      } else set({ error: result.message, starting: false })
    },

    clearStale() {
      set({ stale: null })
    },
  }))
}
