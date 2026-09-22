"use client"

import { useEffect, useState } from "react"

import type { SaveState } from "@/store/save"

/** What autosave is doing, in one line (3 설계 §9).
 *
 * Autosave runs once a minute, so 저장 대기 중 is a state someone actually sits in -- the line says how
 * to leave it.
 *
 * Not decoration: someone who does not know whether their work is saved will not close the tab, and
 * someone who wrongly believes it is will lose it. So the five states are named plainly, and a failure
 * carries the engine's own message rather than a generic one.
 */

const LABELS: Record<SaveState["status"], string> = {
  saved: "저장됨",
  pending: "저장 대기 중",
  saving: "저장 중",
  conflict: "다른 곳에서 변경됨",
  error: "저장 실패",
}

const COLOURS: Record<SaveState["status"], string> = {
  saved: "var(--st-succeeded)",
  pending: "var(--fg-faint)",
  saving: "var(--st-running)",
  conflict: "var(--st-waiting)",
  error: "var(--st-failed)",
}

/** `savedAt` is a timestamp and `now` is a parameter, so this is pure and a test needs no clock. */
export function savedAtText(savedAt: number | null, now: number): string | null {
  if (savedAt === null) return null
  // Negative when the two clocks disagree, which the `< 60` branch answers for anyway -- no clamp.
  const seconds = Math.round((now - savedAt) / 1000)
  if (seconds < 60) return "방금"
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}분 전`
  return `${Math.floor(minutes / 60)}시간 전`
}

/** A clock that advances, so "방금" becomes "2분 전" without anything else changing.
 *
 * Reading `Date.now()` during render would be impure and, worse, would never update: the text would
 * say 방금 for as long as nothing else re-rendered the bar. Half a minute is the right tick -- the text
 * only changes on a minute boundary, so anything faster re-renders for nothing.
 */
function useClock(enabled: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!enabled) return
    const id = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(id)
  }, [enabled])
  return now
}

export function StatusBar({ state, now }: { state: SaveState; now?: number }) {
  // A test passes its own instant rather than waiting for one.
  const ticking = useClock(now === undefined)
  const when = savedAtText(state.savedAt, now ?? ticking)

  return (
    // Named, because the canvas puts its own `role="status"` on screen for edit errors and two
    // unnamed live regions are indistinguishable to a screen reader -- and to a test.
    <div role="status" aria-label="저장 상태" aria-live="polite" className="flex items-center gap-2 text-xs">
      <span aria-hidden className="size-1.5 rounded-full" style={{ background: COLOURS[state.status] }} />
      <span style={{ color: state.status === "error" ? "var(--st-failed)" : "var(--fg-muted)" }}>
        {LABELS[state.status]}
      </span>
      {state.status === "error" && state.error !== null ? (
        <span className="text-fg-faint">{state.error}</span>
      ) : when !== null ? (
        <span className="text-fg-faint">마지막 저장 {when}</span>
      ) : null}
      {/* The wait is a minute, which is long enough to wonder. The way out is named while it applies. */}
      {state.status === "pending" ? <span className="text-fg-faint">Ctrl+C로 지금 저장</span> : null}
    </div>
  )
}
