/** The editor's copy of the workflow DSL.
 *
 * This -- not React Flow's internal node and edge arrays -- is the source of truth (3 설계 §5.1).
 * React Flow renders a view derived from it, and every edit goes through a pure command in
 * `commands.ts`, so undo/redo and autosave are one mechanism rather than three.
 *
 * The shape mirrors `engine/dsl/models.py` with one addition: `position`, which the engine stores but
 * excludes from `dsl_hash`, so moving a node saves without creating a new workflow version.
 */

export interface XY {
  x: number
  y: number
}

/** A node's `config` is whatever its `configSchema` allows; the engine is the authority, not us. */
export type NodeConfig = Record<string, unknown>

export interface RetrySpec {
  maxAttempts?: number
  backoff?: "exponential" | "fixed"
  initialDelaySec?: number
}

export interface Policy {
  timeoutSec?: number
  retry?: RetrySpec
  onError?: "fail" | "default"
  defaultOutput?: unknown
}

export interface EditorNode {
  id: string
  type: string
  label?: string
  config?: NodeConfig
  /** Absent means "no override"; an empty object is NOT the same thing (see `setPolicy`). */
  policy?: Policy
  /** Editor-only. Excluded from `dsl_hash` by the engine. */
  position: XY
}

export interface EditorEdge {
  id: string
  source: string
  sourceHandle?: string
  target: string
  /** Present only on a back-edge; the engine reads it as the loop bound. */
  maxIterations?: number
}

export interface EditorSettings {
  storeRunData?: boolean
}

export interface EditorDsl {
  version: "1"
  settings?: EditorSettings
  nodes: EditorNode[]
  edges: EditorEdge[]
}

/** The engine's default handle name for a node with a single output. */
export const DEFAULT_HANDLE = "out"

/** Node types the engine allows exactly one of, and which therefore carry no number. */
export const SINGLETON_TYPES: ReadonlySet<string> = new Set(["start", "end"])

export function emptyDsl(): EditorDsl {
  return { version: "1", nodes: [], edges: [] }
}

export function findNode(dsl: EditorDsl, nodeId: string): EditorNode | undefined {
  return dsl.nodes.find((node) => node.id === nodeId)
}

/** The handle an edge leaves by, defaulted the way the engine defaults it. */
export function edgeHandle(edge: EditorEdge): string {
  return edge.sourceHandle ?? DEFAULT_HANDLE
}
