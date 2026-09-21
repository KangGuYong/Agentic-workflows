"use client"

import { useRouter } from "next/navigation"
import { useState, useSyncExternalStore } from "react"

import { localTime } from "@/lib/format/time"
import {
  createWorkflow,
  deleteWorkflow,
  listWorkflows,
  renameWorkflow,
  type WorkflowSummary,
} from "@/lib/engine/workflows"

/** The workflow list (Task 19).
 *
 * Rows, not cards. A list of things with the same three facts each is a **table**: aligning name,
 * revision and time in columns lets someone scan down one of them, which is the whole reason to have
 * a list. Cards would put each row's three facts in a different place.
 */
const NEW_NAME = "새 워크플로"

export function WorkflowList({ initial }: { initial: WorkflowSummary[] }) {
  const router = useRouter()
  const [workflows, setWorkflows] = useState(initial)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [renaming, setRenaming] = useState<string | null>(null)

  async function refresh() {
    const result = await listWorkflows()
    if (result.outcome === "ok") setWorkflows(result.workflows)
    else setError(result.message)
  }

  async function onCreate() {
    setBusy(true)
    const result = await createWorkflow(NEW_NAME)
    setBusy(false)
    if (result.outcome === "ok") router.push(`/workflows/${result.value.id}`)
    else setError(result.message)
  }

  return (
    <div>
      <div className="mb-4 flex items-center">
        <button
          type="button"
          onClick={() => void onCreate()}
          disabled={busy}
          className="border px-3 py-1.5 text-xs disabled:opacity-40"
          style={{ borderRadius: "var(--radius)", borderColor: "var(--accent)", color: "var(--accent)" }}
        >
          새 워크플로
        </button>
      </div>

      {error === null ? null : (
        <p role="alert" className="mb-4 border px-3 py-2 text-xs" style={{ borderRadius: "var(--radius)", borderColor: "var(--st-failed)", color: "var(--st-failed)" }}>
          {error}
        </p>
      )}

      {workflows.length === 0 ? (
        <p className="text-sm text-fg-muted">아직 워크플로가 없습니다. 새 워크플로로 시작해 보세요.</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="rule-engraved text-left">
              <th scope="col" className="instrument-label pb-2 font-normal">이름</th>
              <th scope="col" className="instrument-label pb-2 font-normal">버전</th>
              <th scope="col" className="instrument-label pb-2 font-normal">마지막 저장</th>
              <th scope="col" className="sr-only">작업</th>
            </tr>
          </thead>
          <tbody>
            {workflows.map((workflow, index) => (
              <Row
                key={workflow.id}
                workflow={workflow}
                index={index}
                renaming={renaming === workflow.id}
                onRename={() => setRenaming(workflow.id)}
                onRenamed={() => {
                  setRenaming(null)
                  void refresh()
                }}
                onCancelRename={() => setRenaming(null)}
                onError={setError}
                onDeleted={() => void refresh()}
              />
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function Row({
  workflow,
  index,
  renaming,
  onRename,
  onRenamed,
  onCancelRename,
  onError,
  onDeleted,
}: {
  workflow: WorkflowSummary
  /** Only for the staggered entrance; nothing about the row depends on it. */
  index: number
  renaming: boolean
  onRename: () => void
  onRenamed: () => void
  onCancelRename: () => void
  onError: (message: string) => void
  onDeleted: () => void
}) {
  const router = useRouter()
  const [name, setName] = useState(workflow.name)
  const [busy, setBusy] = useState(false)

  async function save() {
    if (name.trim() === "" || name === workflow.name) {
      onCancelRename()
      return
    }
    setBusy(true)
    // `PUT` replaces the row, so the draft has to be fetched and sent back with the new name. Sending
    // an empty document here would wipe the workflow -- the rename is the smallest possible edit.
    const opened = await fetch(`/api/engine/workflows/${encodeURIComponent(workflow.id)}`)
      .then((response) => (response.ok ? (response.json() as Promise<{ draftDsl?: unknown; revision?: number }>) : null))
      .catch(() => null)
    if (opened === null || typeof opened.revision !== "number") {
      setBusy(false)
      onError("이름을 바꾸지 못했습니다. 다시 시도해 주세요.")
      return
    }
    const result = await renameWorkflow(workflow.id, name.trim(), opened.draftDsl ?? {}, opened.revision)
    setBusy(false)
    if (result.outcome === "ok") onRenamed()
    else onError(result.message)
  }

  async function remove() {
    if (!window.confirm(`'${workflow.name}' 워크플로를 삭제할까요? 되돌릴 수 없습니다.`)) return
    setBusy(true)
    const result = await deleteWorkflow(workflow.id)
    setBusy(false)
    if (result.outcome === "ok") onDeleted()
    else onError(result.message)
  }

  return (
    <tr className="panel-enter border-b border-ink-700" style={{ "--row": index } as React.CSSProperties}>
      <td className="py-2 pr-4">
        {renaming ? (
          <input
            autoFocus
            aria-label="이름"
            className="w-full border border-ink-600 bg-ink-700 px-2 py-1 text-sm outline-none focus:border-ink-500"
            style={{ borderRadius: "var(--radius)" }}
            value={name}
            disabled={busy}
            onChange={(event) => setName(event.target.value)}
            onBlur={() => void save()}
            onKeyDown={(event) => {
              if (event.key === "Enter") void save()
              if (event.key === "Escape") {
                setName(workflow.name)
                onCancelRename()
              }
            }}
          />
        ) : (
          <button
            type="button"
            onClick={() => router.push(`/workflows/${workflow.id}`)}
            className="text-left hover:text-accent"
          >
            {workflow.name}
          </button>
        )}
      </td>
      <td className="readout py-2 pr-4 text-xs text-fg-muted">r{workflow.revision}</td>
      <td className="readout py-2 pr-4 text-xs text-fg-faint">
        <Timestamp at={workflow.updatedAt} />
      </td>
      <td className="py-2 text-right">
        <button type="button" onClick={onRename} disabled={busy} className="mr-3 text-xs text-fg-muted underline disabled:opacity-40">
          이름 바꾸기
        </button>
        <button type="button" onClick={() => void remove()} disabled={busy} className="text-xs text-fg-muted underline disabled:opacity-40">
          삭제
        </button>
      </td>
    </tr>
  )
}

/** A local time, rendered on the client only.
 *
 * The server has no idea what timezone the reader is in, so a server-rendered local time disagrees
 * with the browser's and React replaces it on hydration -- a logged mismatch and, for a moment, the
 * wrong time on screen. `useSyncExternalStore` says that plainly: empty on the server, formatted on
 * the client, with no effect and no state to keep in step.
 */
function Timestamp({ at }: { at: string | undefined }) {
  const text = useSyncExternalStore(
    subscribeToNothing,
    () => localTime(at),
    () => "",
  )
  return <span>{text}</span>
}

/** The value never changes after mount, so there is nothing to subscribe to. */
function subscribeToNothing() {
  return () => {}
}
