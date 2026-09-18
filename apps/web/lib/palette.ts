/** The node palette: what `/node-types` returns, grouped the way the canvas shows it.
 *
 * Pure, so the ordering rules are testable without a DOM.
 */

/** One entry of `GET /node-types` (engine `api/routers/node_types.py`). */
export interface NodeType {
  type: string
  label: string
  category: string
  isBranch: boolean
  sideEffects: boolean
  configSchema: Record<string, unknown>
  defaultPolicy: Record<string, unknown> | null
}

/**
 * Palette order, which is a design decision rather than the engine's.
 *
 * `/node-types` returns registry order -- an implementation detail that would reshuffle if someone
 * reordered an import. This is the order a person builds in: the ends of the workflow first, then the
 * model calls that are the point of it, then the plumbing, then effects on the outside world, then the
 * human step.
 */
export const CATEGORY_ORDER = ["IO", "AI", "Logic", "Action", "Human"] as const

/** Anything the editor has not heard of. It is still placeable; it just sorts last. */
export const OTHER_CATEGORY = "기타"

export interface PaletteGroup {
  category: string
  types: NodeType[]
}

export function groupByCategory(types: NodeType[]): PaletteGroup[] {
  const known = new Set<string>(CATEGORY_ORDER)
  const groups: PaletteGroup[] = CATEGORY_ORDER.map((category) => ({
    category,
    // Within a group, the API's order stands: the engine offers no ordering hint, and sorting by the
    // English type name would be arbitrary next to Korean labels.
    types: types.filter((item) => item.category === category),
  }))

  const unknown = types.filter((item) => !known.has(item.category))
  if (unknown.length > 0) groups.push({ category: OTHER_CATEGORY, types: unknown })

  // An empty heading is noise, and the palette is the first thing a new user reads.
  return groups.filter((group) => group.types.length > 0)
}
