"use client"

import { useEffect, useRef, useState } from "react"

import type { Decision } from "@/lib/engine/resume"
import type { WaitingFor } from "@/lib/run/events"

import { JsonValue } from "./JsonValue"

/** The approval a parked run is waiting on (3 설계 §8.4, Task 17).
 *
 * `review` is read-only unless the node's config said `allowEdit`. That is the tenant's decision about
 * this step, not a preference: the engine validates the edited value against the same rule and rejects
 * one it did not invite, so an editor that offered the box anyway would only produce a 422.
 */
export function ApprovalDialog({
  waiting,
  open,
  submitting,
  error,
  onSubmit,
  onDismiss,
}: {
  waiting: WaitingFor
  open: boolean
  submitting: boolean
  error: string | null
  onSubmit: (decision: Decision, comment: string, editedValue?: unknown) => void
  onDismiss: () => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [comment, setComment] = useState("")
  const [edited, setEdited] = useState<string | null>(null)
  const [editError, setEditError] = useState<string | null>(null)

  useEffect(() => {
    const element = dialog.current
    if (element === null) return
    if (open && !element.open) element.showModal()
    if (!open && element.open) element.close()
  }, [open])

  function submit(decision: Decision) {
    if (edited === null) {
      onSubmit(decision, comment)
      return
    }
    try {
      onSubmit(decision, comment, JSON.parse(edited))
      setEditError(null)
    } catch {
      // Not sent. The engine would reject it too, but saying so here keeps the typed text on screen.
      setEditError("고친 값이 올바른 JSON이 아닙니다.")
    }
  }

  return (
    <dialog
      ref={dialog}
      aria-labelledby="approval-title"
      onCancel={(event) => {
        // Esc closes it. Unlike a save conflict, nothing is lost: the run stays parked and the dialog
        // can be reopened from the node.
        event.preventDefault()
        onDismiss()
      }}
      className="m-auto w-96 max-w-full border border-ink-600 bg-ink-800 p-4 text-fg backdrop:bg-black/60"
      style={{ borderRadius: "var(--radius)" }}
    >
      <h2 id="approval-title" className="text-sm font-semibold">
        승인 요청
      </h2>
      <p className="mt-2 whitespace-pre-wrap text-xs text-fg-muted">{waiting.message}</p>

      {waiting.review === undefined || waiting.review === null ? null : (
        <div className="mt-3">
          <p className="instrument-label mb-1">검토할 값</p>
          {waiting.allowEdit ? (
            <textarea
              aria-label="검토할 값"
              rows={6}
              className="w-full border border-ink-600 bg-ink-700 px-2 py-1 font-mono text-xs outline-none focus:border-ink-500"
              style={{ borderRadius: "var(--radius)" }}
              defaultValue={JSON.stringify(waiting.review, null, 2)}
              onChange={(event) => setEdited(event.target.value)}
            />
          ) : (
            // Read-only, and not as a disabled textarea: a greyed-out box invites a click that does
            // nothing. This is a value being shown, so it is shown the way every other value is.
            <div className="max-h-40 overflow-auto border border-ink-600 bg-ink-700 px-2 py-1 text-xs" style={{ borderRadius: "var(--radius)" }}>
              <JsonValue value={waiting.review} />
            </div>
          )}
        </div>
      )}

      <label className="mt-3 block">
        <span className="instrument-label mb-1 block">의견 (선택)</span>
        <input
          className="w-full border border-ink-600 bg-ink-700 px-2 py-1 text-xs outline-none focus:border-ink-500"
          style={{ borderRadius: "var(--radius)" }}
          value={comment}
          onChange={(event) => setComment(event.target.value)}
        />
      </label>

      {editError === null ? null : <p className="mt-2 text-xs text-st-failed">{editError}</p>}
      {error === null ? null : <p className="mt-2 text-xs text-st-failed">{error}</p>}

      <div className="mt-4 flex items-center justify-end gap-2">
        <button type="button" onClick={onDismiss} className="px-2 py-1 text-xs text-fg-muted">
          나중에
        </button>
        <button
          type="button"
          disabled={submitting}
          onClick={() => submit("reject")}
          className="border px-3 py-1.5 text-xs disabled:opacity-40"
          style={{ borderRadius: "var(--radius)", borderColor: "var(--st-failed)", color: "var(--st-failed)" }}
        >
          반려
        </button>
        <button
          type="button"
          disabled={submitting}
          onClick={() => submit("approve")}
          className="border px-3 py-1.5 text-xs disabled:opacity-40"
          style={{ borderRadius: "var(--radius)", borderColor: "var(--st-succeeded)", color: "var(--st-succeeded)" }}
        >
          {submitting ? "보내는 중" : "승인"}
        </button>
      </div>
    </dialog>
  )
}
