"use client"

import {
  Background,
  BackgroundVariant,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Connection as FlowConnection,
  type EdgeChange,
  type NodeChange as RfNodeChange,
} from "@xyflow/react"
import { useCallback, useMemo, useState } from "react"
import { useStore } from "zustand"

import "@xyflow/react/dist/style.css"

import { emptyDsl } from "@/lib/dsl/document"
import { toFlowEdges, toFlowNodes, type NodeChange } from "@/lib/dsl/flow"
import type { NodeType } from "@/lib/palette"
import { createGraphStore, type GraphState } from "@/store/graph"

import { DRAG_TYPE, Palette } from "./Palette"
import { WorkflowNode } from "./WorkflowNode"

const NODE_TYPES = { workflow: WorkflowNode }

export function Canvas({ types }: { types: NodeType[] }) {
  return (
    <ReactFlowProvider>
      <Editor types={types} />
    </ReactFlowProvider>
  )
}

function Editor({ types }: { types: NodeType[] }) {
  // One store per editor screen, created once. React Flow renders a view of its document; it never
  // holds the document itself (3 설계 §5.1).
  const [store] = useState(() => createGraphStore(emptyDsl()))
  const state = useStore(store)
  const { screenToFlowPosition } = useReactFlow()

  const labels = useMemo(
    () => Object.fromEntries(types.map((item) => [item.type, item.label])),
    [types],
  )
  const nodes = useMemo(() => toFlowNodes(state.dsl, { labels }), [state.dsl, labels])
  const edges = useMemo(() => toFlowEdges(state.dsl), [state.dsl])

  const onNodesChange = useCallback(
    (changes: RfNodeChange[]) => {
      state.moveNodes(changes as unknown as NodeChange[])
      const selected = changes.filter((change) => change.type === "select")
      if (selected.length > 0) {
        const chosen = new Set(state.selection.nodes)
        for (const change of selected) {
          if (change.selected) chosen.add(change.id)
          else chosen.delete(change.id)
        }
        state.select({ nodes: [...chosen], edges: state.selection.edges })
      }
    },
    [state],
  )

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      const selected = changes.filter((change) => change.type === "select")
      if (selected.length === 0) return
      const chosen = new Set(state.selection.edges)
      for (const change of selected) {
        if (change.selected) chosen.add(change.id)
        else chosen.delete(change.id)
      }
      state.select({ nodes: state.selection.nodes, edges: [...chosen] })
    },
    [state],
  )

  const onConnect = useCallback(
    (connection: FlowConnection) => {
      state.connectNodes({
        source: connection.source,
        sourceHandle: connection.sourceHandle ?? undefined,
        target: connection.target,
      })
    },
    [state],
  )

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault()
      const type = event.dataTransfer.getData(DRAG_TYPE)
      if (type === "") return
      state.addNodeAt(type, screenToFlowPosition({ x: event.clientX, y: event.clientY }))
    },
    [state, screenToFlowPosition],
  )

  return (
    <div className="flex h-full min-h-0 flex-1">
      <Palette types={types} />
      <div
        className="canvas-grid relative min-w-0 flex-1"
        onDrop={onDrop}
        onDragOver={(event) => {
          event.preventDefault()
          event.dataTransfer.dropEffect = "copy"
        }}
      >
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={NODE_TYPES}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onNodeDragStop={state.endDrag}
          onSelectionDragStop={state.endDrag}
          // React Flow's own delete would mutate its internal state behind the document's back.
          deleteKeyCode={null}
          onKeyDown={(event) => {
            if (event.key === "Delete" || event.key === "Backspace") {
              event.preventDefault()
              state.removeSelected()
            }
          }}
          proOptions={{ hideAttribution: true }}
          fitView
        >
          <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="transparent" />
        </ReactFlow>
        <Toolbar state={state} />
      </div>
    </div>
  )
}

function Toolbar({ state }: { state: GraphState }) {
  return (
    <div className="pointer-events-none absolute left-4 top-4 flex items-center gap-2">
      <div className="pointer-events-auto flex items-center gap-1 border border-ink-600 bg-ink-800 p-1" style={{ borderRadius: "var(--radius)" }}>
        <ToolbarButton label="되돌리기" disabled={!state.canUndo} onClick={state.undo}>
          ↶
        </ToolbarButton>
        <ToolbarButton label="다시하기" disabled={!state.canRedo} onClick={state.redo}>
          ↷
        </ToolbarButton>
      </div>
      {state.lastError !== null ? (
        <p
          className="pointer-events-auto border px-2 py-1 text-xs"
          style={{ borderRadius: "var(--radius)", borderColor: "var(--st-failed)", color: "var(--st-failed)" }}
          role="status"
        >
          {state.lastError}
        </p>
      ) : null}
    </div>
  )
}

function ToolbarButton({
  label,
  disabled,
  onClick,
  children,
}: {
  label: string
  disabled: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      className="readout px-2 py-1 text-sm disabled:opacity-35"
      style={{ borderRadius: "var(--radius)" }}
    >
      {children}
    </button>
  )
}
