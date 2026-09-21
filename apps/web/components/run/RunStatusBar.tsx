"use client"

import { statusById, type StatusId } from "@/lib/design/status"
import type { StreamState } from "@/lib/run/events"

/** What the run being watched is doing (3 설계 §8.1, Task 15).
 *
 * Separate from the save status bar and named separately, because they answer different questions --
 * "is my work safe" and "is my run going" -- and a screen reader hearing two unnamed live regions
 * cannot tell which just changed.
 */

const RUN_STATUS: Record<StreamState["status"], { label: string; status: StatusId }> = {
  queued: { label: "대기 중", status: "queued" },
  running: { label: "실행 중", status: "running" },
  waiting: { label: "승인 대기", status: "waiting" },
  succeeded: { label: "성공", status: "succeeded" },
  failed: { label: "실패", status: "failed" },
  cancelled: { label: "취소됨", status: "queued" },
}

export function RunStatusBar({ stream }: { stream: StreamState }) {
  const shown = RUN_STATUS[stream.status]
  const colour = statusById(shown.status).token

  return (
    <div role="status" aria-label="실행 상태" aria-live="polite" className="flex items-center gap-2 text-xs">
      <span aria-hidden className="size-1.5 rounded-full" style={{ background: `var(${colour})` }} />
      <span className="text-fg-muted">{shown.label}</span>
      {stream.disconnected ? (
        // Said, not acted on: `EventSource` is already backing off and resending `Last-Event-ID`.
        // What someone needs here is to know why the screen stopped moving.
        <span style={{ color: "var(--st-waiting)" }}>연결 끊김 — 재연결 중</span>
      ) : null}
    </div>
  )
}
