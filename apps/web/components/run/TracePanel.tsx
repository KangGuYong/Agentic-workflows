"use client"

import { useState } from "react"

import { statusById, statusOfNodeRun } from "@/lib/design/status"
import { koreanMessage } from "@/lib/errors/korean"
import { durationMs, durationText, hasDetail, runsForNode, type NodeRun } from "@/lib/run/trace"

import { JsonValue } from "./JsonValue"

/** What a node actually did, per attempt (3 설계 §8.5, Task 16).
 *
 * One row per `(execIndex, attempt)`, not one per node. A node retried three times has three rows and
 * a node in a loop has one per iteration, because "it worked" and "it worked on the third try" are
 * different facts about a workflow and collapsing them hides the second.
 */
export function TracePanel({
  nodeId,
  runs,
  hasMore,
  loading = false,
  error = null,
  onLoadMore,
}: {
  nodeId: string
  runs: readonly NodeRun[]
  hasMore: boolean
  loading?: boolean
  error?: string | null
  onLoadMore?: () => void
}) {
  const mine = runsForNode(runs, nodeId)

  if (error !== null) return <p className="text-sm text-st-failed">{error}</p>
  if (loading && mine.length === 0) return <p className="text-sm text-fg-muted">불러오는 중…</p>
  if (mine.length === 0) {
    // Not an empty panel: "nothing here" and "this has not run" look the same and mean different things.
    return <p className="text-sm text-fg-muted">아직 실행되지 않았습니다.</p>
  }

  return (
    <div className="flex flex-col gap-2">
      {mine.map((run) => (
        <Attempt key={`${run.execIndex}-${run.attempt}`} run={run} showIteration={hasIterations(mine)} />
      ))}
      {hasMore ? (
        <button
          type="button"
          onClick={onLoadMore}
          disabled={loading}
          className="self-start border border-ink-600 bg-ink-700 px-2 py-1 text-xs disabled:opacity-40"
          style={{ borderRadius: "var(--radius)" }}
        >
          {loading ? "불러오는 중…" : "더 보기"}
        </button>
      ) : null}
    </div>
  )
}

/** Whether to show the iteration number, which only means something inside a loop. */
function hasIterations(runs: readonly NodeRun[]): boolean {
  return new Set(runs.map((run) => run.execIndex)).size > 1
}

function Attempt({ run, showIteration }: { run: NodeRun; showIteration: boolean }) {
  const [open, setOpen] = useState(false)
  const status = statusById(statusOfNodeRun(run.status))
  const expandable = hasDetail(run)

  return (
    <div className="border border-ink-600" style={{ borderRadius: "var(--radius)" }}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        disabled={!expandable}
        aria-expanded={expandable ? open : undefined}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-xs disabled:cursor-default"
      >
        <span
          aria-label={`상태: ${status.label}`}
          role="img"
          className="flex size-3.5 shrink-0 items-center justify-center text-[9px] text-ink-800"
          style={{ background: `var(${status.token})`, borderRadius: "9999px" }}
        >
          <span aria-hidden>{status.glyph}</span>
        </span>
        <span className="text-fg-muted">
          {showIteration ? `${run.execIndex}회차 · ` : ""}
          {run.attempt > 1 ? `${run.attempt}번째 시도` : "1번째 시도"}
        </span>
        <span className="ml-auto text-fg-faint">{durationText(durationMs(run))}</span>
      </button>

      <div className="flex flex-wrap gap-x-3 px-2 pb-1.5 text-[0.6875rem] text-fg-faint">
        {run.tokensIn > 0 || run.tokensOut > 0 ? (
          <span>
            토큰 {run.tokensIn} → {run.tokensOut}
          </span>
        ) : null}
        {run.truncated ? <span style={{ color: "var(--st-waiting)" }}>값이 잘렸습니다</span> : null}
      </div>

      {open ? (
        <div className="flex flex-col gap-2 border-t border-ink-600 px-2 py-2 text-[0.6875rem]">
          {run.error !== null ? (
            <Section label="오류">
              <p className="text-st-failed">{koreanMessage(run.error.message ?? run.error.code ?? "")}</p>
            </Section>
          ) : null}
          {run.input !== null ? (
            <Section label="입력">
              <JsonValue value={run.input} />
            </Section>
          ) : null}
          {run.output !== null ? (
            <Section label="출력">
              <JsonValue value={run.output} />
            </Section>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="instrument-label mb-0.5">{label}</p>
      {children}
    </div>
  )
}
