import {
  DEFAULT_HANDLE,
  SINGLETON_TYPES,
  edgeHandle,
  type EditorDsl,
  type EditorEdge,
  type EditorNode,
  type NodeConfig,
  type Policy,
  type XY,
} from "./document"
import { nextEdgeId, nextNodeId } from "./ids"

/** Every edit to the workflow document, as pure `(dsl, ...args) => dsl` functions (3 설계 §5.1).
 *
 * Pure and non-mutating is not tidiness: the undo stack keeps whole snapshots, so a command that
 * mutated a node in place would reach back and change the past as well as the present. The commands
 * test freezes its input to make that a thrown error rather than a quiet one.
 *
 * A command that cannot do anything sensible returns the document it was given, unchanged and
 * identical by reference, so the caller can skip pushing a history entry with `before === after`.
 */

function replaceNode(dsl: EditorDsl, nodeId: string, change: (node: EditorNode) => EditorNode): EditorDsl {
  let changed = false
  const nodes = dsl.nodes.map((node) => {
    if (node.id !== nodeId) return node
    changed = true
    return change(node)
  })
  return changed ? { ...dsl, nodes } : dsl
}

/** An object with every undefined-valued key dropped, or undefined if nothing is left. */
function pruned<T extends object>(value: T): T | undefined {
  const entries = Object.entries(value).filter(([, item]) => item !== undefined)
  return entries.length > 0 ? (Object.fromEntries(entries) as T) : undefined
}

export function addNode(dsl: EditorDsl, type: string, position: XY): EditorDsl {
  if (SINGLETON_TYPES.has(type) && dsl.nodes.some((node) => node.type === type)) {
    throw new Error(`${type} 노드는 하나만 놓을 수 있습니다`)
  }
  const node: EditorNode = { id: nextNodeId(type, dsl), type, position }
  return { ...dsl, nodes: [...dsl.nodes, node] }
}

export function removeNode(dsl: EditorDsl, nodeId: string): EditorDsl {
  if (!dsl.nodes.some((node) => node.id === nodeId)) return dsl
  return {
    ...dsl,
    nodes: dsl.nodes.filter((node) => node.id !== nodeId),
    // Both directions: an edge left pointing at a deleted node is a DSL the engine rejects, and the
    // canvas would draw it as a line to nowhere.
    edges: dsl.edges.filter((edge) => edge.source !== nodeId && edge.target !== nodeId),
  }
}

export interface Connection {
  source: string
  sourceHandle?: string
  target: string
}

export function connect(dsl: EditorDsl, connection: Connection): EditorDsl {
  const { source, target } = connection
  const handle = connection.sourceHandle ?? DEFAULT_HANDLE
  if (source === target) return dsl // the engine's graph rules have no room for a self-loop
  const known = new Set(dsl.nodes.map((node) => node.id))
  if (!known.has(source) || !known.has(target)) return dsl
  // An explicit "out" and an absent handle are the same connection, so comparing the defaulted value
  // is what stops the editor creating the duplicate the engine would report as EDGE_DUPLICATE.
  const duplicate = dsl.edges.some(
    (edge) => edge.source === source && edgeHandle(edge) === handle && edge.target === target,
  )
  if (duplicate) return dsl

  const edge: EditorEdge = { id: nextEdgeId(dsl), source, target }
  // Written only when it is not the default: an explicit "out" everywhere would change `dsl_hash` for
  // documents that mean exactly the same thing.
  if (handle !== DEFAULT_HANDLE) edge.sourceHandle = handle
  return { ...dsl, edges: [...dsl.edges, edge] }
}

export function disconnect(dsl: EditorDsl, edgeId: string): EditorDsl {
  if (!dsl.edges.some((edge) => edge.id === edgeId)) return dsl
  return { ...dsl, edges: dsl.edges.filter((edge) => edge.id !== edgeId) }
}

export function setConfig(dsl: EditorDsl, nodeId: string, config: NodeConfig): EditorDsl {
  // Replaces rather than merges: a merge cannot delete a key, so clearing an optional field -- an
  // `llm`'s `outputSchema`, say -- would be impossible.
  return replaceNode(dsl, nodeId, (node) => ({ ...node, config }))
}

export function setLabel(dsl: EditorDsl, nodeId: string, label: string): EditorDsl {
  const trimmed = label.trim()
  return replaceNode(dsl, nodeId, (node) => {
    const { label: _previous, ...rest } = node
    return trimmed === "" ? rest : { ...rest, label: trimmed }
  })
}

export function setPolicy(dsl: EditorDsl, nodeId: string, policy: Policy): EditorDsl {
  const retry = policy.retry === undefined ? undefined : pruned(policy.retry)
  const effective = pruned({ ...policy, retry })
  return replaceNode(dsl, nodeId, (node) => {
    const { policy: _previous, ...rest } = node
    // `dsl_hash` hashes `policy: {}` and a missing policy differently even though both mean "no
    // override", so an empty object would create a workflow version identical to the one before it.
    return effective === undefined ? rest : { ...rest, policy: effective }
  })
}

export function setPositions(dsl: EditorDsl, moved: Record<string, XY>): EditorDsl {
  const ids = Object.keys(moved)
  if (ids.length === 0) return dsl
  let changed = false
  const nodes = dsl.nodes.map((node) => {
    const position = moved[node.id]
    if (position === undefined) return node
    changed = true
    return { ...node, position }
  })
  return changed ? { ...dsl, nodes } : dsl
}
