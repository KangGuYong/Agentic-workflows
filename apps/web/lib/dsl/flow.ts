import { statusOfNodeRun, type StatusId } from "@/lib/design/status"
import type { NodeRunState } from "@/lib/run/events"
import { handleOf, worstOf, type Issue } from "@/store/validation"

import { DEFAULT_HANDLE, edgeHandle, type EditorDsl, type XY } from "./document"

/** The DSL document, as React Flow wants to see it (3 설계 §5.1).
 *
 * One direction only. React Flow renders a *view* of the document; edits come back as commands, not as
 * a React Flow state the document is then derived from. Keeping the mapping here and pure means the one
 * asymmetry that matters -- the default handle -- is written down and tested rather than discovered.
 */

export interface FlowNodeData extends Record<string, unknown> {
  type: string
  label: string
  handles: string[]
  /** What `/validate` says about this node, for its badge. Empty until the first response. */
  issues: Issue[]
  /** Handles a `HANDLE_NOT_CONNECTED` error names, so the node can mark the one that is empty. */
  unconnectedHandles: string[]
  /** How this node is doing in the run being watched, or null when no run is. */
  run: { status: StatusId; tokens: string } | null
}

export interface Size {
  width: number
  height: number
}

export interface FlowNode {
  id: string
  type: "workflow"
  position: XY
  data: FlowNodeData
  /** The size React Flow last measured this node at, handed straight back to it.
   *
   * React Flow is used controlled here, and in controlled mode the node objects it is given are the
   * whole truth: `adoptUserNodes` rebuilds its internal node from each one whenever the object's
   * identity changes, taking `measured` from the object and nothing else. A node arriving without it
   * is treated as never measured -- drawn `visibility: hidden` and with its handle bounds discarded --
   * until a ResizeObserver callback measures it again. That callback never comes when the size has not
   * changed, so a graph re-derived mid-run (a node event, a validation response) can go blank and stay
   * blank. Carrying the measurement back is the contract, not an optimisation.
   */
  measured?: Size
  /** Whether the document's selection holds this node. See `FlowView.selection`. */
  selected: boolean
}

export interface FlowEdge {
  id: string
  source: string
  sourceHandle: string
  target: string
  /** Drawn in the severity's colour when `/validate` has something to say about this edge. */
  style?: { stroke: string }
  /** Whether the document's selection holds this edge. See `FlowView.selection`. */
  selected: boolean
}

/** What is selected, as the graph store holds it: node ids and edge ids. */
export interface FlowSelection {
  nodes: readonly string[]
  edges: readonly string[]
}

export interface FlowView {
  /** Handle names per node from the last successful `/validate`. */
  handles?: Record<string, string[]>
  /** Node-type labels from `/node-types`, keyed by node id's type. */
  labels?: Record<string, string>
  /** The last `/validate` issue list, which the badges and edge colours are drawn from. */
  issues?: readonly Issue[]
  /** Per-node state from the run event stream, keyed by node id. */
  runNodes?: Record<string, NodeRunState>
  /** Sizes React Flow has reported, keyed by node id. See `FlowNode.measured`. */
  measured?: Record<string, Size>
  /** The store's selection, written onto every node and edge as `selected`.
   *
   * Same contract as `measured`: in controlled mode the objects handed to React Flow are the whole
   * truth, and it rebuilds its internal nodes from them on every new array. Left off, React Flow
   * forgets which node is selected the moment the graph is re-derived -- any edit, a validation
   * response, a run event -- and a selection made by the store itself (a node just dropped from the
   * palette is selected so its panel opens) is never known to it at all. Its click handling then
   * goes wrong in a way that looks random: with no node it believes selected there is no `selected:
   * false` change to send for the previous one, so clicking a second node adds to the selection
   * instead of replacing it, two nodes are selected, and the panel -- which opens on exactly one --
   * does not appear. Clicking empty canvas likewise clears nothing.
   */
  selection?: FlowSelection
}

export function toFlowNodes(dsl: EditorDsl, view: FlowView): FlowNode[] {
  const issues = view.issues ?? []
  return dsl.nodes.map((node) => {
    const nodeIssues = issues.filter((issue) => issue.nodeId === node.id)
    const size = view.measured?.[node.id]
    return {
    id: node.id,
    type: "workflow" as const,
    position: node.position,
    // Omitted rather than set to undefined when unknown: React Flow reads `measured?.width`, and an
    // explicit `{width: undefined}` is the same to it as absent but not to a structural comparison.
    ...(size === undefined ? {} : { measured: size }),
    selected: view.selection?.nodes.includes(node.id) ?? false,
    data: {
      type: node.type,
      // The node's own label, then the type's, then the bare type. A node type the editor has never
      // heard of still has to draw something a person can read.
      label: node.label ?? view.labels?.[node.type] ?? node.type,
      // Before the first successful analysis -- and for as long as a freshly dropped node stays
      // unconnected, since HANDLE_NOT_CONNECTED is a phase-2 error that empties the node map -- there is
      // nothing to go on. Without a handle the node could never be connected, so it gets the default.
      handles: view.handles?.[node.id] ?? [DEFAULT_HANDLE],
      issues: nodeIssues,
      // `handles.<name>` is how HANDLE_NOT_CONNECTED addresses a branch output. Marking the handle
      // itself is the difference between "something is wrong with this node" and "connect this one".
      unconnectedHandles: nodeIssues
        .map((issue) => handleOf(issue))
        .filter((handle): handle is string => handle !== null),
      run: runOf(view.runNodes?.[node.id]),
    },
  }
  })
}

function runOf(node: NodeRunState | undefined): { status: StatusId; tokens: string } | null {
  return node === undefined ? null : { status: statusOfNodeRun(node.status), tokens: node.tokens }
}

/** The severity colour an edge is drawn in, or undefined when it has no issues. */
function edgeStroke(issues: readonly Issue[], edgeId: string): { stroke: string } | undefined {
  const worst = worstOf(issues.filter((issue) => issue.edgeId === edgeId))
  if (worst === null) return undefined
  return { stroke: worst === "error" ? "var(--st-failed)" : "var(--st-waiting)" }
}

export function toFlowEdges(dsl: EditorDsl, view: FlowView = {}): FlowEdge[] {
  const issues = view.issues ?? []
  return dsl.edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    // Made explicit here. The DSL omits `sourceHandle: "out"` so that two documents meaning the same
    // thing hash the same; React Flow has no such rule, and an edge with an undefined sourceHandle does
    // not attach to a Handle rendered with id "out" -- it draws from the node's centre instead, which
    // looks like a rendering glitch rather than a mismatch.
    sourceHandle: edgeHandle(edge),
    target: edge.target,
    style: edgeStroke(issues, edge.id),
    selected: view.selection?.edges.includes(edge.id) ?? false,
  }))
}

/** What React Flow reports when nodes move. Only the fields this mapping reads. */
export interface NodeChange {
  id: string
  type: string
  position?: XY
  dimensions?: Size
  [key: string]: unknown
}

/** The sizes React Flow just measured, from the same change list `movedPositions` reads.
 *
 * React Flow reports a measurement once, as a change. Dropping it -- which is what handling only
 * `position` and `select` changes does -- means the next re-derived node claims never to have been
 * measured, and the canvas can go blank. See `FlowNode.measured`.
 */
export function sizeChanges(changes: NodeChange[]): Record<string, Size> {
  const sizes: Record<string, Size> = {}
  for (const change of changes) {
    if (change.type === "dimensions" && change.dimensions !== undefined) sizes[change.id] = change.dimensions
  }
  return sizes
}

export function movedPositions(changes: NodeChange[]): Record<string, XY> {
  const moved: Record<string, XY> = {}
  for (const change of changes) {
    // React Flow emits `{type: "position", dragging: false}` with no position when a drag ends; reading
    // that as a move would write undefined over the node's real coordinates.
    if (change.type === "position" && change.position !== undefined) moved[change.id] = change.position
  }
  return moved
}
