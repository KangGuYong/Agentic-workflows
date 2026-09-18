import type { EditorNode } from "./document"
import { NODE_SIZE } from "./layout"

/** Is any node where the user is looking?
 *
 * The canvas has one failure mode that no unit test of the document can catch: a command that moves
 * every node at once -- auto-layout, its undo, loading a different workflow -- leaves the viewport
 * pointing at where the graph *used* to be, and the screen goes blank. The document is perfectly
 * correct; there is simply nothing in frame. Only opening the page shows it.
 *
 * So the rule is: after the document changes, if nothing is in frame, fit the view. Not "always fit"
 * -- that would yank the viewport on every small undo -- and not "fit after auto-layout", which fixes
 * the one case and leaves its undo broken.
 */

export interface ViewportState {
  viewport: { x: number; y: number; zoom: number }
  width: number
  height: number
}

export function anyNodeVisible(nodes: EditorNode[], view: ViewportState): boolean {
  // Nothing to look at is not a problem to fix, and fitting an empty graph would reset the user's pan
  // every time they cleared the canvas.
  if (nodes.length === 0) return true
  // React Flow reports 0x0 until the container is measured. Reading that as "nothing visible" would
  // fire a fit on mount and fight the component's own `fitView`.
  if (view.width === 0 || view.height === 0) return true

  const { x, y, zoom } = view.viewport
  const left = -x / zoom
  const top = -y / zoom
  const right = (-x + view.width) / zoom
  const bottom = (-y + view.height) / zoom

  // Overlap, not containment: half a node on screen is still on screen.
  return nodes.some(
    (node) =>
      node.position.x + NODE_SIZE.width >= left &&
      node.position.x <= right &&
      node.position.y + NODE_SIZE.height >= top &&
      node.position.y <= bottom,
  )
}
