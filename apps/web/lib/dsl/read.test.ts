import { describe, expect, it } from "vitest"

import { readDocument } from "./read"

const NODE = { id: "start", type: "start", position: { x: 1, y: 2 } }

describe("readDocument", () => {
  it("reads a document the editor itself wrote, unchanged", () => {
    const dsl = { version: "1", nodes: [NODE], edges: [{ id: "e1", source: "a", target: "b" }] }
    const result = readDocument(dsl)

    expect(result).toMatchObject({ ok: true, filledPositions: 0 })
    expect(result.ok && result.dsl).toEqual(dsl)
  })

  it("fills in the positions a draft written elsewhere does not have", () => {
    // Positions are editor-only and excluded from `dsl_hash`, so a draft created through the API has
    // none at all. This is the normal case, not a corrupt one.
    const result = readDocument({
      nodes: [
        { id: "start", type: "start" },
        { id: "llm_1", type: "llm" },
      ],
      edges: [],
    })

    expect(result).toMatchObject({ ok: true, filledPositions: 2 })
    expect(result.ok && result.dsl.nodes.every((node) => Number.isFinite(node.position.x))).toBe(true)
  })

  it("does not stack them on top of each other", () => {
    const result = readDocument({
      nodes: Array.from({ length: 6 }, (_, index) => ({ id: `n${index}`, type: "llm" })),
      edges: [],
    })
    const seen = new Set((result.ok ? result.dsl.nodes : []).map((node) => `${node.position.x},${node.position.y}`))

    expect(seen.size).toBe(6)
  })

  it("keeps the position a node already has", () => {
    const result = readDocument({ nodes: [NODE, { id: "b", type: "llm" }], edges: [] })

    expect(result.ok && result.dsl.nodes[0]?.position).toEqual({ x: 1, y: 2 })
    expect(result.ok && result.filledPositions).toBe(1)
  })

  it("treats a half-written position as missing", () => {
    for (const position of [{ x: 1 }, { x: "1", y: 2 }, { x: NaN, y: 0 }, null, "0,0"]) {
      const result = readDocument({ nodes: [{ id: "a", type: "llm", position }], edges: [] })
      expect(result, JSON.stringify(position)).toMatchObject({ ok: true, filledPositions: 1 })
    }
  })

  it("carries everything it does not name through untouched", () => {
    // `config`, `policy`, `label`, `settings`, and whatever a newer engine adds. Losing any of it would
    // mean the next autosave writes a smaller document than the one that was opened.
    const result = readDocument({
      version: "1",
      settings: { storeRunData: false },
      nodes: [{ ...NODE, label: "시작", config: { inputs: {} }, policy: { timeoutSec: 5 } }],
      edges: [{ id: "e1", source: "a", target: "b", maxIterations: 3 }],
    })

    expect(result.ok && result.dsl.settings).toEqual({ storeRunData: false })
    expect(result.ok && result.dsl.nodes[0]).toMatchObject({ label: "시작", policy: { timeoutSec: 5 } })
    expect(result.ok && result.dsl.edges[0]).toMatchObject({ maxIterations: 3 })
  })

  it("refuses rather than dropping a node it cannot show", () => {
    // A dropped node would be missing from the next autosave, and nobody would know which one.
    for (const nodes of [[{ type: "llm" }], [{ id: "a" }], ["start"], [null]]) {
      expect(readDocument({ nodes, edges: [] }), JSON.stringify(nodes)).toMatchObject({ ok: false })
    }
  })

  it("refuses a document that is not one", () => {
    for (const raw of [null, [], "x", {}, { nodes: [] }, { nodes: {}, edges: [] }]) {
      expect(readDocument(raw), JSON.stringify(raw)).toMatchObject({ ok: false })
    }
  })

  it("says which node or edge it could not read", () => {
    const result = readDocument({ nodes: [NODE, { type: "llm" }], edges: [] })

    expect(result.ok === false && result.reason).toContain("2번째")
  })

  it("refuses an edge with no id, which the canvas cannot key on", () => {
    expect(readDocument({ nodes: [], edges: [{ source: "a", target: "b" }] })).toMatchObject({ ok: false })
  })
})
