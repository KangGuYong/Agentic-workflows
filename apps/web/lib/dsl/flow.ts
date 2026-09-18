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
}

export interface FlowNode {
  id: string
  type: "workflow"
  position: XY
  data: FlowNodeData
}

export interface FlowEdge {
  id: string
  source: string
  sourceHandle: string
  target: string
}

export interface FlowView {
  /** Handle names per node from the last successful `/validate`. */
  handles?: Record<string, string[]>
  /** Node-type labels from `/node-types`, keyed by node id's type. */
  labels?: Record<string, string>
}

export function toFlowNodes(dsl: EditorDsl, view: FlowView): FlowNode[] {
  return dsl.nodes.map((node) => ({
    id: node.id,
    type: "workflow" as const,
    position: node.position,
    data: {
      type: node.type,
      // The node's own label, then the type's, then the bare type. A node type the editor has never
      // heard of still has to draw something a person can read.
      label: node.label ?? view.labels?.[node.type] ?? node.type,
      // Before the first successful analysis -- and for as long as a freshly dropped node stays
      // unconnected, since HANDLE_NOT_CONNECTED is a phase-2 error that empties the node map -- there is
      // nothing to go on. Without a handle the node could never be connected, so it gets the default.
      handles: view.handles?.[node.id] ?? [DEFAULT_HANDLE],
    },
  }))
}

export function toFlowEdges(dsl: EditorDsl): FlowEdge[] {
  return dsl.edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    // Made explicit here. The DSL omits `sourceHandle: "out"` so that two documents meaning the same
    // thing hash the same; React Flow has no such rule, and an edge with an undefined sourceHandle does
    // not attach to a Handle rendered with id "out" -- it draws from the node's centre instead, which
    // looks like a rendering glitch rather than a mismatch.
    sourceHandle: edgeHandle(edge),
    target: edge.target,
  }))
}

/** What React Flow reports when nodes move. Only the fields this mapping reads. */
export interface NodeChange {
  id: string
  type: string
  position?: XY
  [key: string]: unknown
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
