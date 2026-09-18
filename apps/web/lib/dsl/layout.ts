import type { EditorDsl, XY } from "./document"

/** Auto-layout, as two pure mappings around one ELK call.
 *
 * The mappings are separated from the call so the parts that can be wrong -- what ELK is told, and how
 * its answer is read -- are testable without running a layout engine.
 */

/** What ELK is told each node measures. It cannot see the DOM, and every node on this canvas is drawn
 * from the same template, so one estimate covers them: `min-w-[168px]` plus padding, two lines of text. */
export const NODE_SIZE = { width: 180, height: 56 } as const

export interface ElkNode {
  id: string
  width?: number
  height?: number
  x?: number
  y?: number
}

export interface ElkEdge {
  id: string
  sources: string[]
  targets: string[]
}

export interface ElkGraph {
  id: string
  layoutOptions: Record<string, string>
  children: ElkNode[]
  edges: ElkEdge[]
}

export interface ElkResult {
  children?: ElkNode[]
}

const LAYOUT_OPTIONS: Record<string, string> = {
  "elk.algorithm": "layered",
  // Left to right: a workflow reads as a sequence, and every node's inputs are on its left edge.
  "elk.direction": "RIGHT",
  "elk.layered.spacing.nodeNodeBetweenLayers": "80",
  "elk.spacing.nodeNode": "40",
  // Keeps a branch's outputs in the order their handles are drawn rather than reordering to shorten
  // edges, so the canvas matches the panel.
  "elk.layered.crossingMinimization.semiInteractive": "true",
}

export function toElkGraph(dsl: EditorDsl): ElkGraph {
  const known = new Set(dsl.nodes.map((node) => node.id))
  return {
    id: "root",
    layoutOptions: LAYOUT_OPTIONS,
    children: dsl.nodes.map((node) => ({ id: node.id, ...NODE_SIZE })),
    // ELK throws on an edge naming a node it does not have. `removeNode` never leaves one, but an
    // imported or hand-edited document can, and laying out what we can beats crashing the button.
    edges: dsl.edges
      .filter((edge) => known.has(edge.source) && known.has(edge.target))
      .map((edge) => ({ id: edge.id, sources: [edge.source], targets: [edge.target] })),
  }
}

export function positionsFrom(result: ElkResult): Record<string, XY> {
  const positions: Record<string, XY> = {}
  for (const child of result.children ?? []) {
    // ELK's types make x and y optional; a node it could not place would otherwise become
    // `{x: undefined, y: undefined}` and disappear from the canvas.
    if (child.x !== undefined && child.y !== undefined) positions[child.id] = { x: child.x, y: child.y }
  }
  return positions
}

/** Lay the document out. Resolves to the new position of every node ELK placed. */
export async function layout(dsl: EditorDsl): Promise<Record<string, XY>> {
  if (dsl.nodes.length === 0) return {}
  // Imported here rather than at module scope: elk.bundled is ~1.5 MB, and nothing needs it until
  // someone presses 자동 정렬.
  const { default: ELK } = await import("elkjs/lib/elk.bundled.js")
  const elk = new ELK()
  const result = (await elk.layout(toElkGraph(dsl) as never)) as ElkResult
  return positionsFrom(result)
}
