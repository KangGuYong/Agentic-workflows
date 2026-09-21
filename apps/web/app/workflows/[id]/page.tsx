import { notFound } from "next/navigation"

import { Canvas } from "@/components/canvas/Canvas"
import { readDocument } from "@/lib/dsl/read"
import { engineFetch } from "@/lib/engine/client"
import type { NodeType } from "@/lib/palette"

/** The editor screen.
 *
 * The workflow is named by the path, not guessed. Task 12 opened whichever one was most recent
 * because there was no list yet; Task 19 built the list, so this route takes the id it was given and
 * 404s rather than silently opening someone else's workflow.
 */
export const dynamic = "force-dynamic"

interface WorkflowView {
  id: string
  name: string
  revision: number
}

async function openWorkflow(id: string): Promise<{ view: WorkflowView; draft: unknown } | null> {
  try {
    const opened = await engineFetch<WorkflowView & { draftDsl: unknown }>(
      `/workflows/${encodeURIComponent(id)}`,
    )
    const { draftDsl, ...view } = opened
    return { view, draft: draftDsl }
  } catch {
    // `engineFetch` throws on any non-2xx, including the 404 for an id that is not there. Either way
    // this route has no workflow to open, and Next's `notFound()` is the honest answer.
    return null
  }
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
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>
  searchParams: Promise<{ run?: string }>
}) {
  const [{ id }, { run }] = await Promise.all([params, searchParams])
  const [{ nodeTypes }, opened] = await Promise.all([
    engineFetch<{ nodeTypes: NodeType[] }>("/node-types"),
    openWorkflow(id),
  ])
  if (opened === null) notFound()

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
      workflowName={opened.view.name}
      initialDsl={read.dsl}
      initialRevision={opened.view.revision}
      initialRunId={run}
    />
  )
}
