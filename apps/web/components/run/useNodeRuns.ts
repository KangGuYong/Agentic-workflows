"use client"

import { useCallback, useEffect, useState } from "react"

import { fetchNodeRuns } from "@/lib/engine/nodeRuns"
import type { NodeRun } from "@/lib/run/trace"

/** The run's node history, fetched once per run and again when it ends (3 설계 §8.5, Task 16).
 *
 * Not per event: the stream already drives the live status, and refetching on every frame would be a
 * request per token. The rows are the *record* -- inputs, outputs, durations -- which only matters once
 * an attempt is over, so fetching when the run reaches a terminal state is enough.
 *
 * `loading` is **derived**, not stored: it is "we want a page for this key and do not have it yet",
 * which is a fact about the arguments rather than a state to keep in step. Storing it meant setting
 * state synchronously inside the effect, which costs a render pass before paint on every fetch.
 */
export const PAGE_SIZE = 100

const EMPTY: NodeRun[] = []

interface Loaded {
  /** Which (run, limit, finished) this page answers for. */
  key: string
  runs: NodeRun[]
  hasMore: boolean
}

export function useNodeRuns(runId: string | null, finished: boolean) {
  const [limit, setLimit] = useState(PAGE_SIZE)
  const [loaded, setLoaded] = useState<Loaded | null>(null)
  const [failure, setFailure] = useState<{ key: string; message: string } | null>(null)

  const key = `${runId ?? ""}:${limit}:${finished}`
  const loadMore = useCallback(() => setLimit((current) => current + PAGE_SIZE), [])

  useEffect(() => {
    if (runId === null) return
    const controller = new AbortController()

    fetchNodeRuns(runId, { limit, signal: controller.signal })
      .then((page) => {
        if (controller.signal.aborted) return
        setLoaded({ key, runs: page.nodeRuns, hasMore: page.hasMore })
        setFailure(null)
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted) return
        // The rows already on screen stay: a failed refresh is not evidence the history is gone.
        setFailure({ key, message: caught instanceof Error ? caught.message : "실행 기록을 불러오지 못했습니다" })
      })

    return () => controller.abort()
    // `finished` is in `key` on purpose: the run ending is the moment the record is complete.
  }, [runId, limit, key])

  if (runId === null) {
    return { runs: EMPTY, hasMore: false, loading: false, error: null, loadMore }
  }

  const answered = loaded?.key === key || failure?.key === key
  return {
    // The previous page while a newer one is on its way: 더 보기 should extend the list, not blank it.
    runs: loaded?.runs ?? EMPTY,
    hasMore: loaded?.hasMore ?? false,
    loading: !answered,
    error: failure?.key === key ? failure.message : null,
    loadMore,
  }
}
