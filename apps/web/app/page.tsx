import { Canvas } from "@/components/canvas/Canvas"
import { readDocument } from "@/lib/dsl/read"
import { engineFetch } from "@/lib/engine/client"
import type { NodeType } from "@/lib/palette"

/** The editor screen.
 *
 * Opening a workflow here is deliberately the smallest thing that works: the id from the URL, else the
 * most recently touched one, else a new one. **Task 19 replaces this with a real list** -- naming,
 * choosing, deleting. What it cannot be is nothing: autosave (Task 12) has no meaning without a
 * workflow to save into, and `/validate` (Task 13) is keyed by the same id.
 */
export const dynamic = "force-dynamic"

interface WorkflowView {
  id: string
  name: string
  revision: number
}

const NEW_NAME = "새 워크플로"

async function openWorkflow(id: string | undefined): Promise<{ view: WorkflowView; draft: unknown }> {
  if (id === undefined) {
    const { workflows } = await engineFetch<{ workflows: WorkflowView[] }>("/workflows")
    const recent = workflows[0]
    if (recent === undefined) {
      const created = await engineFetch<WorkflowView>("/workflows", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ name: NEW_NAME }),
      })
      return { view: created, draft: { version: "1", nodes: [], edges: [] } }
    }
    id = recent.id
  }
  const opened = await engineFetch<WorkflowView & { draftDsl: unknown }>(`/workflows/${encodeURIComponent(id)}`)
  const { draftDsl, ...view } = opened
  return { view, draft: draftDsl }
}

/** A draft the editor cannot read is shown, not opened.
 *
 * Opening an editor on an empty document here would autosave that emptiness over the tenant's draft
 * within the second. Refusing costs them an edit; guessing costs them the workflow.
 */
function Unreadable({ name, reason }: { name: string; reason: string }) {
  return (
    <main className="mx-auto max-w-lg p-8">
      <h1 className="text-sm font-semibold">{name}</h1>
      <p className="mt-3 text-sm text-fg-muted">{reason}</p>
      <p className="mt-2 text-xs text-fg-faint">
        내용을 보호하기 위해 편집기를 열지 않았습니다. 이 워크플로는 저장되지 않습니다.
      </p>
    </main>
  )
}

export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ workflow?: string; run?: string }>
}) {
  const { workflow, run } = await searchParams
  const [{ nodeTypes }, opened] = await Promise.all([
    engineFetch<{ nodeTypes: NodeType[] }>("/node-types"),
    openWorkflow(workflow),
  ])

  // An empty draft is what a workflow that has never been saved carries; that is a new document, not
  // an unreadable one.
  const read = readDocument(
    opened.draft === null || opened.draft === undefined
      ? { version: "1", nodes: [], edges: [] }
      : opened.draft,
  )
  if (!read.ok) return <Unreadable name={opened.view.name} reason={read.reason} />

  return (
    <Canvas
      types={nodeTypes}
      workflowId={opened.view.id}
      initialDsl={read.dsl}
      initialRevision={opened.view.revision}
      initialRunId={run}
    />
  )
}
