import type { RunView } from "@/lib/run/restore"

/** `GET /runs/{id}` through the BFF proxy (3 설계 §8.3, Task 18).
 *
 * A 404 is a distinct answer, not a failure: `?run=` survives in a URL someone bookmarked or shared,
 * and the run it names can be gone. That has to be told apart from "the engine is unreachable", where
 * the right thing is to keep waiting rather than to clear the parameter.
 */
export type RunDetail = { outcome: "run"; run: RunView } | { outcome: "gone" } | { outcome: "failed"; message: string }

export async function fetchRun(runId: string, init: { signal?: AbortSignal } = {}): Promise<RunDetail> {
  let response: Response
  try {
    response = await fetch(`/api/engine/runs/${encodeURIComponent(runId)}`, { signal: init.signal })
  } catch (error) {
    return { outcome: "failed", message: error instanceof Error ? error.message : "실행을 불러오지 못했습니다" }
  }

  if (response.status === 404) return { outcome: "gone" }
  if (!response.ok) return { outcome: "failed", message: `실행을 불러오지 못했습니다 (${response.status})` }

  const body: unknown = await response.json().catch(() => null)
  const status = (body as { status?: unknown } | null)?.status
  if (typeof status !== "string") return { outcome: "failed", message: "실행 응답을 이해하지 못했습니다" }

  const view = body as Record<string, unknown>
  return {
    outcome: "run",
    run: {
      id: typeof view["id"] === "string" ? view["id"] : runId,
      status,
      cancelRequested: view["cancelRequested"] === true,
      waitingFor: view["waitingFor"],
      error: view["error"],
    },
  }
}
