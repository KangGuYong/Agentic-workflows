"use client"

import { useEffect, useState } from "react"

import { fetchNodeRuns } from "@/lib/engine/nodeRuns"
import { fetchRun } from "@/lib/engine/runDetail"
import type { StreamState } from "@/lib/run/events"
import { restoreStream } from "@/lib/run/restore"

/** Rebuilding a run from the URL before any stream opens (3 설계 §8.3, Task 18).
 *
 * `state` is `undefined` while the fetch is in flight, and callers must tell that apart from `null`:
 * opening a stream during the fetch would paint the first live frame onto state saying the run never
 * happened. A run started in this session needs no restore -- the stream built its state from the
 * first frame -- so only a run id that arrived in the URL is fetched.
 *
 * "In flight" is **derived** from whether the stored answer is for this run, not stored as a flag.
 * Storing it would mean a `setState` in the effect body, and the first render after `runId` appears
 * would still carry the old `null` -- a single frame in which a stream opens against empty state,
 * which is the whole thing this hook exists to prevent.
 */
export interface Restored {
  /** `undefined` = still fetching; `null` = nothing to restore. */
  state: StreamState | null | undefined
  /** The run in the URL is gone. The caller clears the parameter and says so. */
  gone: boolean
  error: string | null
}

interface Answer extends Restored {
  runId: string
}

const NOTHING: Restored = { state: null, gone: false, error: null }
const FETCHING: Restored = { state: undefined, gone: false, error: null }

export function useRestoredRun(runId: string | null): Restored {
  const [answer, setAnswer] = useState<Answer | null>(null)

  useEffect(() => {
    if (runId === null) return
    const controller = new AbortController()

    void (async () => {
      const detail = await fetchRun(runId, { signal: controller.signal })
      if (controller.signal.aborted) return

      if (detail.outcome === "gone") {
        setAnswer({ runId, state: null, gone: true, error: null })
        return
      }
      if (detail.outcome === "failed") {
        // Not `gone`: clearing `?run=` because the engine is unreachable would throw away the only
        // reference to a run that is probably still going.
        setAnswer({ runId, state: null, gone: false, error: detail.message })
        return
      }

      // The rows may fail while the run itself loaded. The canvas is better off with statuses it can
      // paint than with nothing because one of two requests did not land.
      const page = await fetchNodeRuns(runId, { signal: controller.signal }).catch(() => ({
        nodeRuns: [],
        hasMore: false,
      }))
      if (controller.signal.aborted) return
      setAnswer({ runId, state: restoreStream(detail.run, page.nodeRuns), gone: false, error: null })
    })()

    return () => controller.abort()
  }, [runId])

  if (runId === null) return NOTHING
  return answer?.runId === runId ? answer : FETCHING
}
