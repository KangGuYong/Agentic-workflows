"use client"

import Form from "@rjsf/core"
import type { IChangeEvent } from "@rjsf/core"
import validator from "@rjsf/validator-ajv8"
import { useEffect, useRef, useState } from "react"

import { TEMPLATES } from "@/components/panel/templates"
import type { JsonSchema } from "@/lib/template/schema"

/** The dialog 실행 opens (3 설계 §8, Task 14).
 *
 * RJSF over the `start` node's `inputs` schema -- the same renderer the settings panel uses, because
 * filling values against a schema is the same job there and here. The schema is the tenant's own, so
 * everything about the form comes from what they declared.
 *
 * Validation here is **not** display-only, unlike the settings panel: a run either has its inputs or it
 * does not, and sending a missing required value only to be refused by the engine wastes a round trip
 * and puts the error somewhere harder to read.
 */
export function RunDialog({
  schema,
  open,
  starting,
  error,
  onSubmit,
  onCancel,
}: {
  schema: JsonSchema
  open: boolean
  starting: boolean
  error: string | null
  onSubmit: (inputs: Record<string, unknown>) => void
  onCancel: () => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [formData, setFormData] = useState<Record<string, unknown>>({})

  useEffect(() => {
    const element = dialog.current
    if (element === null) return
    if (open && !element.open) element.showModal()
    if (!open && element.open) element.close()
  }, [open])

  return (
    <dialog
      ref={dialog}
      aria-labelledby="run-title"
      onCancel={(event) => {
        // Esc should close it -- nothing is lost and nothing has started. Unlike the save conflict,
        // this dialog holds no decision that has to be made.
        event.preventDefault()
        onCancel()
      }}
      className="m-auto w-80 max-w-full border border-ink-600 bg-ink-800 p-4 text-fg backdrop:bg-black/60"
      style={{ borderRadius: "var(--radius)" }}
    >
      <h2 id="run-title" className="text-sm font-semibold">
        실행 입력
      </h2>
      <p className="mt-1 text-xs text-fg-muted">시작 노드가 요구하는 값입니다.</p>

      <Form
        schema={schema}
        formData={formData}
        validator={validator}
        templates={TEMPLATES}
        showErrorList={false}
        onChange={(event: IChangeEvent) => setFormData(event.formData as Record<string, unknown>)}
        onSubmit={(event: IChangeEvent) => onSubmit((event.formData ?? {}) as Record<string, unknown>)}
        className="mt-3"
      >
        {error === null ? null : <p className="mb-2 text-xs text-st-failed">{error}</p>}
        <div className="flex items-center justify-end gap-2">
          <button type="button" onClick={onCancel} className="px-2 py-1 text-xs text-fg-muted">
            취소
          </button>
          <button
            type="submit"
            disabled={starting}
            className="border border-ink-600 bg-ink-700 px-3 py-1.5 text-xs disabled:opacity-40"
            style={{ borderRadius: "var(--radius)" }}
          >
            {starting ? "시작하는 중" : "실행"}
          </button>
        </div>
      </Form>
    </dialog>
  )
}
