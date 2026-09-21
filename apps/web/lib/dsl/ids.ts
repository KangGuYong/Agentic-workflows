import { SINGLETON_TYPES, type EditorDsl } from "./document"

/** Node and edge id generation (MVP 설계 4.2).
 *
 * `<type>_<n>`, and **a number is never reused**. `node_runs.node_id` and every stored `run_events` row
 * keep pointing at an id after its node is deleted, so handing `llm_2` to a new node would make it
 * inherit a deleted node's run history in the trace panel. Numbering from max + 1 rather than count + 1
 * is the whole of that guarantee.
 */

/** `<type>_<digits>` with no leading zero, so `llm_01` is not read as 1 and cannot collide with it. */
function numberOf(id: string, type: string): number | null {
  if (!id.startsWith(`${type}_`)) return null
  const tail = id.slice(type.length + 1)
  if (!/^(0|[1-9]\d*)$/.test(tail)) return null
  return Number(tail)
}

function highest(ids: string[], read: (id: string) => number | null): number {
  return ids.reduce((max, id) => {
    const value = read(id)
    return value !== null && value > max ? value : max
  }, 0)
}

export function nextNodeId(type: string, dsl: EditorDsl): string {
  if (SINGLETON_TYPES.has(type)) return type
  const taken = new Set(dsl.nodes.map((node) => node.id))
  // Parsing `<type>_<n>` picks a *tidy* number; the loop is what makes not colliding a guarantee rather
  // than something inferred from the format rules. An imported or hand-edited document can hold an id
  // this numbering would otherwise pick -- `llm_1` on a node of another type, say.
  let number = highest([...taken], (id) => numberOf(id, type)) + 1
  while (taken.has(`${type}_${number}`)) number += 1
  return `${type}_${number}`
}

/** Edge ids are `e<n>` -- no underscore -- so they read their number directly rather than through
 * `numberOf`, which expects the `<type>_<n>` shape. */
function edgeNumber(id: string): number | null {
  const match = /^e(0|[1-9]\d*)$/.exec(id)
  return match === null ? null : Number(match[1])
}

export function nextEdgeId(dsl: EditorDsl): string {
  const taken = new Set(dsl.edges.map((edge) => edge.id))
  let number = highest([...taken], edgeNumber) + 1
  while (taken.has(`e${number}`)) number += 1
  return `e${number}`
}
