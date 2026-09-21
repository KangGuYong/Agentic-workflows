"use client"

import { useRouter } from "next/navigation"
import { useRef, useState, useSyncExternalStore } from "react"

import { downloadText } from "@/lib/browser/download"
import { exportFileName, importedName, parseImport, serialize, MAX_IMPORT_BYTES } from "@/lib/dsl/transfer"
import { localTime } from "@/lib/format/time"
import {
  createWorkflow,
  deleteWorkflow,
  listWorkflows,
  openDraft,
  putWorkflow,
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

  /** Import always makes a **new** workflow; it never replaces an open one.
   *
   * A file picker is one mis-click away from the wrong file, and replacing a draft with it would be a
   * destructive action with no undo. Creating means the worst case is one workflow to delete.
   */
  async function onImport(file: File) {
    setError(null)
    // The `File`'s own size first, so a huge file is never read into a string at all.
    if (file.size > MAX_IMPORT_BYTES) {
      setError(`파일이 너무 큽니다 (최대 ${Math.floor(MAX_IMPORT_BYTES / 1024)}KB).`)
      return
    }
    setBusy(true)
    const text = await file.text().catch(() => null)
    const read = text === null ? { ok: false as const, reason: "파일을 읽지 못했습니다." } : parseImport(text)
    if (!read.ok) {
      setBusy(false)
      setError(read.reason)
      return
    }

    const name = importedName(file.name)
    const created = await createWorkflow(name)
    if (created.outcome !== "ok") {
      setBusy(false)
      setError(created.message)
      return
    }
    // Two calls because `POST` takes only a name: the document goes in the `PUT` that follows. If this
    // one fails the empty workflow is left behind rather than silently removed -- deleting on a failed
    // save is how an import that actually half-succeeded disappears without a trace.
    const saved = await putWorkflow(created.value.id, name, read.dsl, created.value.revision)
    setBusy(false)
    if (saved.outcome !== "ok") {
      setError(`${saved.message} '${name}' 워크플로는 비어 있는 채로 만들어졌습니다.`)
      void refresh()
      return
    }
    router.push(`/workflows/${created.value.id}`)
  }

  return (
    <div>
      <div className="mb-4 flex items-center gap-2">
        <button
          type="button"
          onClick={() => void onCreate()}
          disabled={busy}
          className="border px-3 py-1.5 text-xs disabled:opacity-40"
          style={{ borderRadius: "var(--radius)", borderColor: "var(--accent)", color: "var(--accent)" }}
        >
          새 워크플로
        </button>
        <ImportButton busy={busy} onPick={(file) => void onImport(file)} />
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

/** A file picker that looks like the buttons beside it.
 *
 * A bare `<input type="file">` cannot be styled to match, so the input is hidden and a real button
 * opens it. Hidden with `sr-only` rather than `display: none`, so it keeps its accessible name and a
 * screen reader can still reach it directly.
 *
 * The value is cleared after each pick: picking the *same* file twice in a row fires no `change` event
 * otherwise, which looks exactly like the import silently failing.
 */
function ImportButton({ busy, onPick }: { busy: boolean; onPick: (file: File) => void }) {
  const input = useRef<HTMLInputElement>(null)

  return (
    <>
      <input
        ref={input}
        type="file"
        accept="application/json,.json"
        aria-label="워크플로 파일"
        className="sr-only"
        onChange={(event) => {
          const file = event.target.files?.[0]
          event.target.value = ""
          if (file !== undefined) onPick(file)
        }}
      />
      <button
        type="button"
        onClick={() => input.current?.click()}
        disabled={busy}
        className="border border-ink-600 px-3 py-1.5 text-xs text-fg-muted disabled:opacity-40"
        style={{ borderRadius: "var(--radius)" }}
      >
        가져오기
      </button>
    </>
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
    const opened = await openDraft(workflow.id)
    if (opened.outcome !== "ok") {
      setBusy(false)
      onError("이름을 바꾸지 못했습니다. 다시 시도해 주세요.")
      return
    }
    const result = await putWorkflow(workflow.id, name.trim(), opened.value.draftDsl ?? {}, opened.value.revision)
    setBusy(false)
    if (result.outcome === "ok") onRenamed()
    else onError(result.message)
  }

  async function exportFile() {
    setBusy(true)
    const opened = await openDraft(workflow.id)
    setBusy(false)
    if (opened.outcome !== "ok") {
      onError(opened.message)
      return
    }
    // The stored draft, written out as-is. Not re-read through `readDocument` first: export should
    // hand back what is stored, and filling in positions here would change the file a round trip
    // produces.
    downloadText(exportFileName(workflow.name), serialize(opened.value.draftDsl))
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
        <button type="button" onClick={() => void exportFile()} disabled={busy} className="mr-3 text-xs text-fg-muted underline disabled:opacity-40">
          내보내기
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
