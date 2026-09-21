"use client"

import { Handle, Position, type NodeProps } from "@xyflow/react"

import { Badge } from "@/components/validation/Badge"
import type { FlowNodeData } from "@/lib/dsl/flow"
import { worstOf } from "@/store/validation"

/** One node on the canvas.
 *
 * Instrument-panel chrome: a hairline-ruled plate, the label in the body face, the id etched below it
 * in mono. The id matters more than it looks -- it is what every template reference, every `node_runs`
 * row and every event names -- so it is always visible rather than hidden behind a selection.
 *
 * Contrary to the frontend-design skill's "no cards, no boxes" rule, which is for scroll-driven
 * marketing pages: a node without a boundary cannot carry a validation badge or a run status, and on a
 * grid canvas it would read as floating text (계획서 「시각 디자인」).
 */
export function WorkflowNode({ data, selected }: NodeProps) {
  const { type, label, handles, issues, unconnectedHandles } = data as FlowNodeData
  const severity = worstOf(issues)
  // The issue colour wins over the selection outline: a node can be both, and "this is broken" is the
  // more urgent of the two to see at a glance.
  const border =
    severity === "error"
      ? "var(--st-failed)"
      : severity === "warning"
        ? "var(--st-waiting)"
        : selected === true
          ? "var(--accent)"
          : "var(--ink-600)"

  return (
    <div
      className="relative min-w-[168px] border bg-ink-800 px-3 py-2 transition-colors"
      style={{
        borderRadius: "var(--radius)",
        borderColor: border,
        boxShadow: selected === true ? `0 0 0 1px ${border}` : "0 1px 0 rgba(0,0,0,0.35)",
      }}
    >
      {severity !== null ? (
        <span className="absolute -right-1.5 -top-1.5">
          <Badge issues={issues} severity={severity} />
        </span>
      ) : null}

      <Handle type="target" position={Position.Left} id="in" style={HANDLE} />

      <p className="text-sm font-semibold leading-tight">{label}</p>
      <p className="instrument-label mt-1">{type}</p>

      {handles.map((handle, index) => (
        <Handle
          key={handle}
          type="source"
          position={Position.Right}
          id={handle}
          style={{
            ...HANDLE,
            top: handleTop(index, handles.length),
            // `HANDLE_NOT_CONNECTED` names one output. Marking the node alone would say "something is
            // wrong here"; marking the handle says "connect this one".
            ...(unconnectedHandles.includes(handle)
              ? { borderColor: "var(--st-failed)", background: "var(--st-failed)" }
              : {}),
          }}
        >
          {handles.length > 1 ? <span className="pointer-events-none absolute left-3 -top-2 whitespace-nowrap text-[0.625rem] text-fg-faint">{handle}</span> : null}
        </Handle>
      ))}
    </div>
  )
}

const HANDLE = {
  width: 8,
  height: 8,
  borderRadius: 2,
  background: "var(--ink-900)",
  border: "1px solid var(--ink-500)",
}

/** Spread several source handles down the right edge, evenly, rather than stacking them at the middle. */
function handleTop(index: number, count: number): string {
  return count === 1 ? "50%" : `${((index + 1) / (count + 1)) * 100}%`
}
