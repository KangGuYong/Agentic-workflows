/** The six run states a node can be in, as the editor draws them.
 *
 * Colour is never the only carrier: each status also has a glyph, because a red/amber pair at badge
 * size is exactly what a red-green colour vision deficiency (and a cheap monitor) flattens. The token
 * names are CSS custom properties from `app/globals.css`; nothing outside that file hard-codes a hex.
 */
export type StatusId = "queued" | "running" | "succeeded" | "failed" | "waiting" | "default"

export interface Status {
  id: StatusId
  /** Korean, because this is what a user reads. */
  label: string
  /** The CSS custom property holding this status' colour. */
  token: `--st-${StatusId}`
  /** Shape carried alongside the colour. */
  glyph: string
}

export const STATUSES: readonly Status[] = [
  { id: "queued", label: "대기", token: "--st-queued", glyph: "○" },
  { id: "running", label: "실행 중", token: "--st-running", glyph: "◍" },
  { id: "succeeded", label: "성공", token: "--st-succeeded", glyph: "●" },
  { id: "failed", label: "실패", token: "--st-failed", glyph: "✕" },
  { id: "waiting", label: "승인 대기", token: "--st-waiting", glyph: "❙❙" },
  { id: "default", label: "기본값", token: "--st-default", glyph: "◌" },
]

export function statusById(id: StatusId): Status {
  const found = STATUSES.find((status) => status.id === id)
  // noUncheckedIndexedAccess makes the compiler ask; the union type makes this unreachable in practice.
  if (found === undefined) throw new Error(`unknown status: ${id}`)
  return found
}
