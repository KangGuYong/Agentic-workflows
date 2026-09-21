import { ranges } from "@/lib/template/parse"

import { SINGLETON_TYPES, type EditorDsl, type EditorEdge, type EditorNode, type XY } from "./document"
import { nextEdgeId, nextNodeId } from "./ids"

/** Placing another document's nodes into the one being edited (Task 24).
 *
 * Not a merge of two workflows: the file's nodes are *added* to this document, the way dropping a node
 * from the palette adds one. Everything that makes that non-trivial is here.
 *
 * **The start and end nodes never come.** `engine/validator/structure.py` fixes their ids (`start`,
 * `end`), so a document can hold exactly one of each; bringing the file's would be a guaranteed
 * `DUPLICATE_NODE_ID`. They are dropped along with the edges that touch them, and the caller says so.
 *
 * **References follow renamed nodes.** A node id is not decoration -- it is what `{{ llm_1.text }}`,
 * every edge and every `node_runs` row names. Renaming `llm_1` to `llm_3` without rewriting the
 * templates that point at it would leave a document that validates as broken in a way nobody asked
 * for. That rewriting is the bulk of this file.
 */

export interface InsertResult {
  dsl: EditorDsl
  /** Nodes actually placed. */
  inserted: number
  /** Ids of the file's singleton nodes that were left behind, in document order. */
  dropped: string[]
  /** Old id -> new id, for every node that had to be renamed to avoid a collision. */
  renamed: Record<string, string>
}

/** Gap between what is already on the canvas and the block being placed. */
const GAP = 120

export function insertDocument(dsl: EditorDsl, incoming: EditorDsl): InsertResult {
  const dropped = incoming.nodes.filter((node) => SINGLETON_TYPES.has(node.type)).map((node) => node.id)
  const carried = incoming.nodes.filter((node) => !SINGLETON_TYPES.has(node.type))
  const kept = new Set(carried.map((node) => node.id))

  // Renames are decided first, against a document that grows as they are handed out: two incoming
  // nodes that would both become `llm_2` must not.
  const renamed: Record<string, string> = {}
  let reserving = dsl
  for (const node of carried) {
    if (!reserving.nodes.some((existing) => existing.id === node.id)) continue
    const id = nextNodeId(node.type, reserving)
    renamed[node.id] = id
    // A placeholder in the growing document, so the next call sees this id as taken.
    reserving = { ...reserving, nodes: [...reserving.nodes, { ...node, id }] }
  }

  const offset = offsetFor(dsl, carried)
  const rename = (id: string): string => renamed[id] ?? id

  const nodes: EditorNode[] = carried.map((node) => ({
    ...node,
    id: rename(node.id),
    position: { x: node.position.x + offset.x, y: node.position.y + offset.y },
    ...(node.config === undefined ? {} : { config: rewriteValue(node.config, renamed) as typeof node.config }),
  }))

  // Edge ids are renumbered unconditionally: they are not referenced by anything, and `e1` colliding
  // is far likelier than a node id colliding.
  const withNodes: EditorDsl = { ...dsl, nodes: [...dsl.nodes, ...nodes] }
  const edges: EditorEdge[] = []
  for (const edge of incoming.edges) {
    // An edge to a node that did not come has nowhere to attach.
    if (!kept.has(edge.source) || !kept.has(edge.target)) continue
    const id = nextEdgeId({ ...withNodes, edges: [...withNodes.edges, ...edges] })
    edges.push({ ...edge, id, source: rename(edge.source), target: rename(edge.target) })
  }

  return {
    dsl: { ...withNodes, edges: [...withNodes.edges, ...edges] },
    inserted: nodes.length,
    dropped,
    renamed,
  }
}

/** Where to put the incoming block: to the right of everything already here, shape intact.
 *
 * The block keeps its internal layout -- one offset for all of it -- because that layout is what the
 * file's author arranged, and 자동 정렬 is there for anyone who would rather have it redone.
 */
function offsetFor(dsl: EditorDsl, carried: readonly EditorNode[]): XY {
  if (carried.length === 0) return { x: 0, y: 0 }
  const incomingLeft = Math.min(...carried.map((node) => node.position.x))
  const incomingTop = Math.min(...carried.map((node) => node.position.y))
  if (dsl.nodes.length === 0) return { x: 80 - incomingLeft, y: 80 - incomingTop }
  const right = Math.max(...dsl.nodes.map((node) => node.position.x))
  const top = Math.min(...dsl.nodes.map((node) => node.position.y))
  return { x: right + GAP + 200 - incomingLeft, y: top - incomingTop }
}

/** Rewrite node references inside any JSON value a config can hold. */
function rewriteValue(value: unknown, renamed: Record<string, string>): unknown {
  if (Object.keys(renamed).length === 0) return value
  if (typeof value === "string") return rewriteReferences(value, renamed)
  if (Array.isArray(value)) return value.map((item) => rewriteValue(item, renamed))
  if (typeof value === "object" && value !== null) {
    // Keys are field names, never node ids, so only values are rewritten.
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, rewriteValue(item, renamed)]))
  }
  return value
}

/** Rewrite the node ids named inside a template's `{{ }}` references.
 *
 * Only inside the references, and only outside string literals: a node called `total` must not turn
 * the word "total" in surrounding prose -- or in `default('total')` -- into `total_2`.
 */
export function rewriteReferences(text: string, renamed: Record<string, string>): string {
  if (Object.keys(renamed).length === 0 || !text.includes("{{")) return text
  let out = ""
  let last = 0
  for (const range of ranges(text)) {
    const opened = text.indexOf("{{", range.start) + 2
    const closed = text.lastIndexOf("}}", range.end)
    out += text.slice(last, opened) + rewriteExpression(text.slice(opened, closed), renamed)
    last = closed
  }
  return out + text.slice(last)
}

const IDENTIFIER = /[A-Za-z_][A-Za-z0-9_]*/y

function rewriteExpression(expression: string, renamed: Record<string, string>): string {
  let out = ""
  let index = 0
  while (index < expression.length) {
    const char = expression[index] as string
    if (char === '"' || char === "'") {
      const end = endOfString(expression, index, char)
      out += expression.slice(index, end)
      index = end
      continue
    }
    IDENTIFIER.lastIndex = index
    const match = IDENTIFIER.exec(expression)
    if (match === null) {
      out += char
      index += 1
      continue
    }
    const word = match[0]
    // Only a *leading* identifier names a node: in `a.b`, `b` is a field. Checking the character
    // before is what keeps `llm_1.llm_1` from having its field renamed too.
    const isField = expression[index - 1] === "."
    out += isField ? word : (renamed[word] ?? word)
    index += word.length
  }
  return out
}

function endOfString(text: string, from: number, quote: string): number {
  let index = from + 1
  while (index < text.length) {
    if (text[index] === "\\") {
      index += 2
      continue
    }
    if (text[index] === quote) return index + 1
    index += 1
  }
  return text.length
}

/** What an insert did, in one line a person can read.
 *
 * Renames and dropped nodes are stated rather than left to be discovered. A rename rewrites the
 * references in the file's own templates, so the document that arrived is not byte-for-byte the one
 * on disk -- saying so is the difference between a helpful tool and a surprising one.
 */
export function describeInsert(result: InsertResult): string {
  if (result.inserted === 0) {
    return result.dropped.length > 0
      ? "놓을 노드가 없습니다. 파일에 시작·끝 노드만 있습니다."
      : "놓을 노드가 없습니다."
  }
  const parts = [`노드 ${result.inserted}개를 놓았습니다.`]
  if (result.dropped.length > 0) parts.push(`파일의 ${result.dropped.join("·")} 노드는 이미 있어 제외했습니다.`)
  const renames = Object.entries(result.renamed)
  if (renames.length > 0) {
    const shown = renames.slice(0, 3).map(([from, to]) => `${from}→${to}`).join(", ")
    const rest = renames.length > 3 ? ` 외 ${renames.length - 3}개` : ""
    parts.push(`id가 겹쳐 이름을 바꿨습니다: ${shown}${rest}.`)
  }
  parts.push("되돌리기로 한 번에 취소할 수 있습니다.")
  return parts.join(" ")
}
