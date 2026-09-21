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
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useStore } from "zustand"

import "@xyflow/react/dist/style.css"

import { emptyDsl, type EditorDsl } from "@/lib/dsl/document"
import { toFlowEdges, toFlowNodes, type NodeChange } from "@/lib/dsl/flow"
import { anyNodeVisible } from "@/lib/dsl/viewport"
import type { NodeType } from "@/lib/palette"
import { saveDraft } from "@/lib/engine/save"
import { startRun } from "@/lib/engine/run"
import { validateDraft } from "@/lib/engine/validate"
import { inputSchema, needsInputs } from "@/lib/run/inputs"
import { createGraphStore, type GraphState } from "@/store/graph"
import { createSaveStore, type SaveState } from "@/store/save"
import type { StreamState } from "@/lib/run/events"
import { createRunStore } from "@/store/run"
import { createValidationStore, workflowIssues, type Issue } from "@/store/validation"

import { NodePanel } from "@/components/panel/NodePanel"
import { ConflictDialog } from "@/components/save/ConflictDialog"
import { StatusBar } from "@/components/save/StatusBar"
import { RunDialog } from "@/components/run/RunDialog"
import { useRunStream } from "@/components/run/useRunStream"
import { RunStatusBar } from "@/components/run/RunStatusBar"
import { Banner } from "@/components/validation/Banner"
import { RunButton } from "@/components/validation/RunButton"

import { DRAG_TYPE, Palette } from "./Palette"
import { WorkflowNode } from "./WorkflowNode"

const NODE_TYPES = { workflow: WorkflowNode }

export interface CanvasProps {
  types: NodeType[]
  /** Absent means nothing is saved -- the fixtures and tests that mount the canvas on its own. */
  workflowId?: string
  initialDsl?: EditorDsl
  initialRevision?: number
}

export function Canvas(props: CanvasProps) {
  return (
    <ReactFlowProvider>
      <Editor {...props} />
    </ReactFlowProvider>
  )
}

function Editor({ types, workflowId, initialDsl, initialRevision = 0 }: CanvasProps) {
  // One store per editor screen, created once. React Flow renders a view of its document; it never
  // holds the document itself (3 설계 §5.1).
  const [store] = useState(() => createGraphStore(initialDsl ?? emptyDsl()))
  const state = useStore(store)
  const saveStore = useSaveStore(store, workflowId, initialRevision)
  const save = useStore(saveStore)
  const validationStore = useValidationStore(workflowId)
  const validation = useStore(validationStore)
  const runStore = useRunStore(workflowId)
  const run = useStore(runStore)
  const [askingInputs, setAskingInputs] = useState(false)
  useAutosave(state.dsl, save.changed)
  useValidate(state.dsl, workflowId, validation.validate)
  useRunInUrl(run.runId)
  const stream = useRunStream(run.runId)

  // The engine's own verdict, when it disagreed with the editor's last `/validate`. Shown as badges
  // like any other issue, because that is where they can be acted on. Memoised: it feeds the node and
  // edge mappings, which would otherwise rebuild on every render.
  const issues: readonly Issue[] = useMemo(
    () => (run.rejected.length > 0 ? [...validation.issues, ...run.rejected] : validation.issues),
    [validation.issues, run.rejected],
  )

  function onRun() {
    // Nothing to ask for: a dialog with no fields is a dialog asking nothing.
    if (!needsInputs(state.dsl)) void run.start({}, save.revision)
    else setAskingInputs(true)
  }
  const { screenToFlowPosition, fitView, getViewport } = useReactFlow()
  const surface = useRef<HTMLDivElement>(null)

  const labels = useMemo(
    () => Object.fromEntries(types.map((item) => [item.type, item.label])),
    [types],
  )
  const handles = useMemo(
    () =>
      validation.nodes === null
        ? undefined
        : Object.fromEntries(Object.entries(validation.nodes).map(([id, analysis]) => [id, analysis.handles])),
    [validation.nodes],
  )
  const nodes = useMemo(
    () => toFlowNodes(state.dsl, { labels, handles, issues, runNodes: stream.nodes }),
    [state.dsl, labels, handles, issues, stream.nodes],
  )
  // The panel opens on exactly one node; a multi-select has nothing single to configure.
  const selected =
    state.selection.nodes.length === 1
      ? state.dsl.nodes.find((node) => node.id === state.selection.nodes[0])
      : undefined
  const byType = useMemo(() => new Map(types.map((item) => [item.type, item])), [types])
  const edges = useMemo(() => toFlowEdges(state.dsl, { issues }), [state.dsl, issues])

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

  const onAutoLayout = useCallback(async () => {
    await state.autoLayout()
    // Auto-layout always reframes: even when a node happens to stay in view, the new arrangement is
    // what the user asked to see.
    await fitView({ duration: 200, padding: 0.2 })
  }, [state, fitView])

  // The safety net. A command that moves every node at once -- auto-layout, its *undo*, loading another
  // workflow -- leaves the viewport where the graph used to be and the canvas goes blank. The document
  // is correct; there is just nothing in frame, which no test of the document can catch. Both bugs
  // showed up only on opening the page.
  useEffect(() => {
    const box = surface.current?.getBoundingClientRect()
    const view = { viewport: getViewport(), width: box?.width ?? 0, height: box?.height ?? 0 }
    if (!anyNodeVisible(state.dsl.nodes, view)) void fitView({ duration: 200, padding: 0.2 })
  }, [state.dsl, getViewport, fitView])

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
        ref={surface}
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
        <Toolbar
          state={state}
          save={save}
          issues={issues}
          runId={run.runId}
          stream={stream}
          onRun={onRun}
          onAutoLayout={onAutoLayout}
        />
      </div>
      {selected !== undefined ? (
        <NodePanel
          node={selected}
          nodeType={byType.get(selected.type)}
          types={types}
          issues={issues}
          state={state}
        />
      ) : null}
      <ConflictDialog state={save} />
      {inputSchema(state.dsl) === null ? null : (
        <RunDialog
          schema={inputSchema(state.dsl) ?? {}}
          open={askingInputs}
          starting={run.starting}
          error={run.error}
          onSubmit={(inputs) => {
            setAskingInputs(false)
            void run.start(inputs, save.revision)
          }}
          onCancel={() => setAskingInputs(false)}
        />
      )}
    </div>
  )
}

/** The save slice, bound to this editor's document and workflow.
 *
 * Created once, like the graph store. Without a workflow id there is nothing to save into -- the
 * fixtures mount the canvas that way -- so the request reports a failure rather than calling an
 * endpoint that would 404, and the status bar says so instead of claiming 저장됨.
 */
function useSaveStore(store: ReturnType<typeof createGraphStore>, workflowId: string | undefined, revision: number) {
  const [saveStore] = useState(() =>
    createSaveStore({
      revision,
      getDsl: () => store.getState().dsl,
      onReload: (dsl) => store.getState().replaceDocument(dsl),
      save: (body) =>
        workflowId === undefined
          ? Promise.resolve({ outcome: "failed" as const, message: "열린 워크플로가 없습니다" })
          : saveDraft(workflowId, body),
    }),
  )
  return saveStore
}

/** Tell the save slice the document changed -- but not about the document it started with.
 *
 * `dsl` is a new object on every edit and the same one otherwise, so the identity is the signal. The
 * first render is skipped deliberately: opening a workflow is not an edit, and saving on open would
 * bump the revision of every workflow anybody looked at.
 */
function useAutosave(dsl: EditorDsl, changed: () => void) {
  const opened = useRef(dsl)
  useEffect(() => {
    if (dsl === opened.current) return
    changed()
  }, [dsl, changed])
}

function Toolbar({
  state,
  save,
  issues,
  runId,
  stream,
  onRun,
  onAutoLayout,
}: {
  state: GraphState
  save: SaveState
  issues: readonly Issue[]
  runId: string | null
  stream: StreamState
  onRun: () => void
  onAutoLayout: () => Promise<void>
}) {
  return (
    <div className="pointer-events-none absolute left-4 top-4 flex flex-col items-start gap-2">
    <div className="flex items-center gap-2">
      <div className="pointer-events-auto flex items-center gap-1 border border-ink-600 bg-ink-800 p-1" style={{ borderRadius: "var(--radius)" }}>
        <ToolbarButton label="되돌리기" disabled={!state.canUndo} onClick={state.undo}>
          ↶
        </ToolbarButton>
        <ToolbarButton label="다시하기" disabled={!state.canRedo} onClick={state.redo}>
          ↷
        </ToolbarButton>
        <span className="mx-1 h-4 w-px bg-ink-600" aria-hidden />
        <button
          type="button"
          onClick={() => void onAutoLayout()}
          disabled={state.layingOut || state.dsl.nodes.length === 0}
          className="px-2 py-1 text-xs disabled:opacity-35"
          style={{ borderRadius: "var(--radius)" }}
        >
          자동 정렬
        </button>
      </div>
      <div className="pointer-events-auto border border-ink-600 bg-ink-800 px-2 py-1.5" style={{ borderRadius: "var(--radius)" }}>
        <StatusBar state={save} />
      </div>
      {runId === null ? null : (
        <div className="pointer-events-auto border border-ink-600 bg-ink-800 px-2 py-1.5" style={{ borderRadius: "var(--radius)" }}>
          <RunStatusBar stream={stream} />
        </div>
      )}
      <RunButton issues={issues} nodeCount={state.dsl.nodes.length} onRun={onRun} />
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
    {/* Problems that belong to no node: a badge on an arbitrary one would send someone to fix a node
        that is fine. */}
    <Banner issues={workflowIssues(issues)} />
    </div>
  )
}

/** The validation slice, bound to this workflow.
 *
 * Without a workflow id there is nothing to validate against -- `/validate` is keyed by one -- so the
 * request reports nothing rather than calling an endpoint that would 404.
 */
function useValidationStore(workflowId: string | undefined) {
  const [validationStore] = useState(() =>
    createValidationStore((dsl) =>
      workflowId === undefined
        ? Promise.resolve({ issues: [] })
        : validateDraft(workflowId, dsl),
    ),
  )
  return validationStore
}

/** Re-validate when the document changes.
 *
 * On the opening document too, unlike autosave: opening is not an edit, but a workflow saved with
 * problems has them on open, and a canvas that looks clean until the first keystroke is lying.
 */
function useValidate(dsl: EditorDsl, workflowId: string | undefined, validate: (dsl: EditorDsl) => Promise<void>) {
  useEffect(() => {
    if (workflowId === undefined) return
    // Same wait as autosave, for the same reason: `/validate` is the editor's hottest endpoint and a
    // request per keystroke is a request per keystroke.
    const timer = setTimeout(() => void validate(dsl), VALIDATE_DELAY_MS)
    return () => clearTimeout(timer)
  }, [dsl, workflowId, validate])
}

const VALIDATE_DELAY_MS = 700

/** The run slice, bound to this workflow. */
function useRunStore(workflowId: string | undefined) {
  const [runStore] = useState(() =>
    createRunStore((body) =>
      workflowId === undefined
        ? Promise.resolve({ outcome: "failed" as const, message: "열린 워크플로가 없습니다" })
        : startRun(workflowId, body),
    ),
  )
  return runStore
}

/** Put the run in the URL (3 설계 §8.3), so reloading the tab comes back to the same run.
 *
 * `replaceState`, not a navigation: the run did not change which page this is, and pushing a history
 * entry would make the browser's back button undo a run, which it cannot.
 */
function useRunInUrl(runId: string | null) {
  useEffect(() => {
    if (runId === null) return
    const url = new URL(window.location.href)
    if (url.searchParams.get("run") === runId) return
    url.searchParams.set("run", runId)
    window.history.replaceState(null, "", url)
  }, [runId])
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
