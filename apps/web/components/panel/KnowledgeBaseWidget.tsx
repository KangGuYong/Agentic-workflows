"use client"

import type { WidgetProps } from "@rjsf/utils"
import { useEffect, useState } from "react"

import { listKnowledgeBases, type KnowledgeBaseSummary } from "@/lib/engine/knowledgeBases"

/** The picker behind `x-knowledge-base` (knowledge-base design §7).
 *
 * It fetches the list itself rather than having the canvas thread it through: the list is small,
 * changes rarely, and only this widget wants it. A saved id that is no longer listed stays selected
 * and is labelled as gone -- silently switching the node to another knowledge base would be worse.
 */
export function KnowledgeBaseWidget({ id, value, disabled, readonly, onChange, onBlur }: WidgetProps) {
  const [bases, setBases] = useState<KnowledgeBaseSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    // Inlined rather than calling a named loader: an effect that hands off to a setState-setter trips
    // `react-hooks/set-state-in-effect`, which can't see the fetch's `await` in between.
    void listKnowledgeBases().then((result) => {
      if (cancelled) return
      if (result.outcome === "ok") setBases(result.knowledgeBases)
      else {
        setError(result.message)
        setBases([])
      }
    })
    return () => {
      cancelled = true
    }
  }, [])

  const current = typeof value === "string" ? value : ""
  const known = bases?.some((kb) => kb.id === current) ?? true

  return (
    <div>
      <select
        id={id}
        value={current}
        disabled={disabled === true || readonly === true || bases === null}
        onChange={(event) => onChange(event.target.value === "" ? undefined : event.target.value)}
        onBlur={() => onBlur(id, value)}
        className="w-full border border-ink-600 bg-ink-700 px-2 py-1.5 text-sm outline-none focus:border-ink-500"
        style={{ borderRadius: "var(--radius)" }}
      >
        <option value="">{bases === null ? "불러오는 중…" : "지식베이스를 고르세요"}</option>
        {current !== "" && !known ? <option value={current}>삭제된 지식베이스 ({current})</option> : null}
        {(bases ?? []).map((kb) => (
          <option key={kb.id} value={kb.id}>
            {kb.name}
          </option>
        ))}
      </select>
      {error === null ? null : <p className="mt-1 text-xs text-st-failed">{error}</p>}
      {error === null && bases !== null && bases.length === 0 ? (
        <p className="mt-1 text-xs text-fg-faint">지식베이스가 없습니다. 지식베이스 화면에서 먼저 만들어 주세요.</p>
      ) : null}
    </div>
  )
}
