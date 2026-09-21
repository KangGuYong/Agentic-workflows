import { Shell } from "@/components/shell/Shell"
import { WorkflowList } from "@/components/workflows/WorkflowList"
import { engineFetch } from "@/lib/engine/client"
import type { WorkflowSummary } from "@/lib/engine/workflows"

/** The workflow list, and the first screen anyone sees (Task 19).
 *
 * Server-rendered from the engine so the list is on screen in one round trip rather than after a
 * client fetch; the client component takes over for everything that changes it.
 */
export const dynamic = "force-dynamic"

export default async function Page() {
  let workflows: WorkflowSummary[] = []
  let error: string | null = null
  try {
    const result = await engineFetch<{ workflows: WorkflowSummary[] }>("/workflows")
    workflows = result.workflows
  } catch {
    // The shell and the 새 워크플로 button still work, and the client can retry. A thrown page would
    // leave someone with nothing to do about it.
    error = "워크플로 목록을 불러오지 못했습니다. 엔진에 연결할 수 없습니다."
  }

  return (
    <Shell title="워크플로">
      {error === null ? null : (
        <p role="alert" className="mb-4 border px-3 py-2 text-xs" style={{ borderRadius: "var(--radius)", borderColor: "var(--st-failed)", color: "var(--st-failed)" }}>
          {error}
        </p>
      )}
      <WorkflowList initial={workflows} />
    </Shell>
  )
}
