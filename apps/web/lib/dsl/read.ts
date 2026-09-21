import type { EditorDsl, EditorEdge, EditorNode, XY } from "./document"

/** Reading a draft the editor did not write (3 설계 §5.1).
 *
 * A draft is arbitrary JSON as far as the engine is concerned -- it stores work in progress and does
 * not require it to validate. So the editor opens drafts written by the API, by an older editor, or by
 * a newer one. The common case is a draft with **no `position`**: positions are editor-only and the
 * engine excludes them from `dsl_hash`, so a draft created any other way simply has none. Crashing on
 * that is not an option; neither is guessing at a document whose shape is wrong.
 *
 * Hence two outcomes and no third. A readable draft comes back with positions filled in and everything
 * else byte-for-byte. An unreadable one comes back as a **refusal**, and the caller must not open an
 * editor on it -- an editor that opened an empty document here would autosave that emptiness over the
 * tenant's draft within the second.
 */

export type ReadResult =
  | { ok: true; dsl: EditorDsl; filledPositions: number }
  | { ok: false; reason: string }

/** Where a node with no position goes. A column, so nothing overlaps and nothing is off-screen; the
 * 자동 정렬 button then lays it out properly. */
function placeAt(index: number): XY {
  return { x: 80 + (index % 4) * 240, y: 80 + Math.floor(index / 4) * 140 }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function isPosition(value: unknown): value is XY {
  return isObject(value) && Number.isFinite(value["x"]) && Number.isFinite(value["y"])
}

export function readDocument(raw: unknown): ReadResult {
  if (!isObject(raw)) return { ok: false, reason: "워크플로 내용을 읽을 수 없습니다." }
  const { nodes, edges } = raw
  if (!Array.isArray(nodes) || !Array.isArray(edges)) {
    return { ok: false, reason: "워크플로에 노드 또는 연결 목록이 없습니다." }
  }

  const read: EditorNode[] = []
  let filledPositions = 0
  for (const [index, node] of nodes.entries()) {
    if (!isObject(node) || typeof node["id"] !== "string" || typeof node["type"] !== "string") {
      // Refuse rather than drop. A node the editor silently discarded would be gone from the next
      // autosave, and nobody would know which one.
      return { ok: false, reason: `${index + 1}번째 노드의 형식을 알 수 없습니다.` }
    }
    const position = node["position"]
    if (isPosition(position)) {
      read.push(node as unknown as EditorNode)
    } else {
      filledPositions += 1
      read.push({ ...(node as unknown as EditorNode), position: placeAt(index) })
    }
  }

  for (const [index, edge] of edges.entries()) {
    if (!isObject(edge) || typeof edge["id"] !== "string") {
      return { ok: false, reason: `${index + 1}번째 연결의 형식을 알 수 없습니다.` }
    }
  }

  return {
    ok: true,
    // Everything not named here rides along untouched: `settings`, and any key a newer engine added.
    dsl: { ...(raw as unknown as EditorDsl), version: "1", nodes: read, edges: edges as EditorEdge[] },
    filledPositions,
  }
}
