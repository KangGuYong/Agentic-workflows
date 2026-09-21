import type { NodeRunPage } from "@/lib/run/trace"

/** `GET /runs/{id}/nodes` through the BFF proxy (3 설계 §8.5, Task 16).
 *
 * One row per **attempt**. `hasMore` comes from the engine fetching one row beyond the limit, so it is
 * a fact rather than an estimate -- the panel can offer 더 보기 without guessing.
 */
export async function fetchNodeRuns(
  runId: string,
  options: { limit?: number; signal?: AbortSignal } = {},
): Promise<NodeRunPage> {
  const query = options.limit === undefined ? "" : `?limit=${encodeURIComponent(String(options.limit))}`
  const response = await fetch(
    `/api/engine/runs/${encodeURIComponent(runId)}/nodes${query}`,
    { signal: options.signal },
  )
  if (!response.ok) throw new Error(`실행 기록을 불러오지 못했습니다 (${response.status})`)

  const body: unknown = await response.json()
  const nodeRuns = (body as { nodeRuns?: unknown } | null)?.nodeRuns
  if (!Array.isArray(nodeRuns)) throw new Error("실행 기록 응답을 이해하지 못했습니다")

  return {
    nodeRuns: nodeRuns as NodeRunPage["nodeRuns"],
    hasMore: (body as { hasMore?: unknown }).hasMore === true,
  }
}
