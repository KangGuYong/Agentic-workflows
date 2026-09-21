import type { EditorDsl } from "@/lib/dsl/document"
import type { NodeType } from "@/lib/palette"

import type { RefNode } from "./complete"
import type { JsonSchema } from "./schema"

/** Building the autocomplete's view of the workflow from the document and `/validate` (3 설계 §4.1).
 *
 * Two sources, and which one answers what matters. **Labels come from the document** -- they change as
 * the person types, and waiting for a round trip to rename a chip would make renaming feel broken.
 * **Schemas and the guaranteed set come from the engine**, because both depend on graph analysis the
 * editor deliberately does not reimplement.
 */

/** The `nodes` map of a `/validate` response. */
export interface NodeAnalysis {
  variables: string[]
  outputSchema: JsonSchema
  handles: string[]
}

export interface TemplateContext {
  nodes: RefNode[]
  guaranteed: string[] | null
}

/**
 * @param analysis the last `/validate` response's `nodes`, or `null` before one has arrived
 * @param forNodeId the node being edited, whose guaranteed set is the one that applies
 */
export function templateContext(
  dsl: EditorDsl,
  types: readonly NodeType[],
  analysis: Record<string, NodeAnalysis> | null,
  forNodeId: string,
): TemplateContext {
  const labels = new Map(types.map((type) => [type.type, type.label]))
  const nodes: RefNode[] = dsl.nodes.map((node) => ({
    id: node.id,
    label: node.label ?? labels.get(node.type) ?? node.type,
    // `{}` is "unknown", which is what the engine calls a schema it cannot narrow -- the honest answer
    // until validation has run, and it simply yields no field suggestions.
    outputSchema: analysis?.[node.id]?.outputSchema ?? {},
  }))
  return {
    nodes,
    // `null`, not `[]`. An empty list means "the engine guarantees nothing", which would mark every
    // candidate 기본값 필요; `null` means "not asked yet" and marks none.
    guaranteed: analysis?.[forNodeId]?.variables ?? null,
  }
}
