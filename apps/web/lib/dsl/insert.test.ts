import { describe, expect, it } from "vitest"

import type { EditorDsl } from "./document"
import { describeInsert, insertDocument, rewriteReferences } from "./insert"

function dsl(nodes: EditorDsl["nodes"], edges: EditorDsl["edges"] = []): EditorDsl {
  return { version: "1", nodes, edges }
}

const OPEN = dsl(
  [
    { id: "start", type: "start", position: { x: 0, y: 0 } },
    { id: "end", type: "end", position: { x: 400, y: 0 } },
  ],
  [{ id: "e1", source: "start", target: "end" }],
)

const FILE = dsl(
  [
    { id: "start", type: "start", position: { x: 0, y: 0 } },
    { id: "template_1", type: "template", position: { x: 200, y: 0 }, config: { template: "{{ start.name }}" } },
    { id: "end", type: "end", position: { x: 400, y: 0 }, config: { outputs: { out: "{{ template_1.text }}" } } },
  ],
  [
    { id: "e1", source: "start", target: "template_1" },
    { id: "e2", source: "template_1", target: "end" },
  ],
)

describe("insertDocument", () => {
  it("adds the file's nodes to the open document", () => {
    const result = insertDocument(OPEN, FILE)

    expect(result.inserted).toBe(1)
    expect(result.dsl.nodes.map((node) => node.id)).toEqual(["start", "end", "template_1"])
  })

  it("leaves the file's start and end behind", () => {
    // The engine fixes their ids, so a document holds exactly one of each; bringing the file's would
    // be a guaranteed DUPLICATE_NODE_ID.
    const result = insertDocument(OPEN, FILE)

    expect(result.dropped).toEqual(["start", "end"])
    expect(result.dsl.nodes.filter((node) => node.type === "start")).toHaveLength(1)
    expect(result.dsl.nodes.filter((node) => node.type === "end")).toHaveLength(1)
  })

  it("drops the edges that touched them, keeping the rest", () => {
    const result = insertDocument(OPEN, FILE)

    // start→template_1 and template_1→end both lost an end, so neither can be placed.
    expect(result.dsl.edges).toEqual(OPEN.edges)
  })

  it("keeps an edge whose both ends came", () => {
    const chain = dsl(
      [
        { id: "template_1", type: "template", position: { x: 0, y: 0 }, config: { template: "a" } },
        { id: "llm_1", type: "llm", position: { x: 200, y: 0 }, config: { prompt: "{{ template_1.text }}" } },
      ],
      [{ id: "e1", source: "template_1", target: "llm_1" }],
    )

    const result = insertDocument(OPEN, chain)

    expect(result.dsl.edges).toHaveLength(2)
    expect(result.dsl.edges.at(-1)).toMatchObject({ source: "template_1", target: "llm_1" })
  })

  it("renames a node whose id is already taken", () => {
    const open = dsl([{ id: "template_1", type: "template", position: { x: 0, y: 0 }, config: { template: "mine" } }])
    const file = dsl([{ id: "template_1", type: "template", position: { x: 0, y: 0 }, config: { template: "theirs" } }])

    const result = insertDocument(open, file)

    expect(result.renamed).toEqual({ template_1: "template_2" })
    expect(result.dsl.nodes.map((node) => node.id)).toEqual(["template_1", "template_2"])
    // The one already here is untouched.
    expect(result.dsl.nodes[0]?.config).toEqual({ template: "mine" })
  })

  it("gives two colliding incoming nodes two different ids", () => {
    const open = dsl([{ id: "llm_1", type: "llm", position: { x: 0, y: 0 } }])
    const file = dsl([
      { id: "llm_1", type: "llm", position: { x: 0, y: 0 } },
      { id: "llm_2", type: "llm", position: { x: 200, y: 0 } },
    ])

    const result = insertDocument(open, file)

    const ids = result.dsl.nodes.map((node) => node.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it("rewrites references and edges to a renamed node", () => {
    const open = dsl([{ id: "template_1", type: "template", position: { x: 0, y: 0 } }])
    const file = dsl(
      [
        { id: "template_1", type: "template", position: { x: 0, y: 0 }, config: { template: "x" } },
        { id: "llm_1", type: "llm", position: { x: 200, y: 0 }, config: { prompt: "앞: {{ template_1.text }}" } },
      ],
      [{ id: "e9", source: "template_1", target: "llm_1" }],
    )

    const result = insertDocument(open, file)

    const llm = result.dsl.nodes.find((node) => node.id === "llm_1")
    expect(llm?.config).toEqual({ prompt: "앞: {{ template_2.text }}" })
    expect(result.dsl.edges[0]).toMatchObject({ source: "template_2", target: "llm_1" })
  })

  it("renumbers edge ids so an incoming e1 cannot collide", () => {
    const chain = dsl(
      [
        { id: "template_1", type: "template", position: { x: 0, y: 0 } },
        { id: "llm_1", type: "llm", position: { x: 200, y: 0 } },
      ],
      [{ id: "e1", source: "template_1", target: "llm_1" }],
    )

    const result = insertDocument(OPEN, chain)

    const ids = result.dsl.edges.map((edge) => edge.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it("places the block clear of what is already on the canvas, shape intact", () => {
    const file = dsl([
      { id: "template_1", type: "template", position: { x: 1000, y: 40 } },
      { id: "llm_1", type: "llm", position: { x: 1200, y: 140 } },
    ])

    const result = insertDocument(OPEN, file)

    const placed = result.dsl.nodes.filter((node) => node.type !== "start" && node.type !== "end")
    const [first, second] = placed
    // Right of the rightmost existing node...
    expect(first?.position.x).toBeGreaterThan(400)
    // ...and the block's own shape is unchanged.
    expect((second?.position.x ?? 0) - (first?.position.x ?? 0)).toBe(200)
    expect((second?.position.y ?? 0) - (first?.position.y ?? 0)).toBe(100)
  })

  it("does not touch the document it was given", () => {
    const open = dsl([{ id: "template_1", type: "template", position: { x: 0, y: 0 } }])
    Object.freeze(open)
    Object.freeze(open.nodes)

    expect(() => insertDocument(open, FILE)).not.toThrow()
    expect(open.nodes).toHaveLength(1)
  })

  it("inserts into an empty canvas at the usual starting corner", () => {
    const result = insertDocument(dsl([]), dsl([{ id: "llm_1", type: "llm", position: { x: 900, y: 900 } }]))

    expect(result.dsl.nodes[0]?.position).toEqual({ x: 80, y: 80 })
  })
})

describe("rewriteReferences", () => {
  const RENAMED = { llm_1: "llm_7", total: "total_2" }

  it("rewrites the node a reference names", () => {
    expect(rewriteReferences("{{ llm_1.text }}", RENAMED)).toBe("{{ llm_7.text }}")
  })

  it("leaves text outside a reference alone", () => {
    // The word is the node's id, but prose is prose.
    expect(rewriteReferences("total 합계: {{ total.value }}", RENAMED)).toBe("total 합계: {{ total_2.value }}")
  })

  it("leaves a string literal inside a reference alone", () => {
    expect(rewriteReferences("{{ llm_1.text | default('llm_1') }}", RENAMED)).toBe(
      "{{ llm_7.text | default('llm_1') }}",
    )
  })

  it("rewrites a node named anywhere in the expression, not only first", () => {
    expect(rewriteReferences("{{ a.b | default(llm_1.text) }}", RENAMED)).toBe("{{ a.b | default(llm_7.text) }}")
  })

  it("does not rewrite a field that happens to share the name", () => {
    // `x.llm_1` is a field of `x`, not the node.
    expect(rewriteReferences("{{ x.llm_1 }}", RENAMED)).toBe("{{ x.llm_1 }}")
  })

  it("leaves a reference to a node that was not renamed", () => {
    expect(rewriteReferences("{{ start.name }}", RENAMED)).toBe("{{ start.name }}")
  })

  it("handles several references in one field", () => {
    expect(rewriteReferences("{{ llm_1.a }} / {{ llm_1.b }}", RENAMED)).toBe("{{ llm_7.a }} / {{ llm_7.b }}")
  })

  it("returns the text unchanged when nothing was renamed", () => {
    const text = "{{ llm_1.text }}"

    expect(rewriteReferences(text, {})).toBe(text)
  })
})

describe("describeInsert", () => {
  const base = { dsl: dsl([]), inserted: 0, dropped: [], renamed: {} }

  it("counts what was placed", () => {
    expect(describeInsert({ ...base, inserted: 3 })).toContain("노드 3개를 놓았습니다")
  })

  it("names the nodes left behind", () => {
    // Otherwise someone imports a whole workflow and quietly gets it without its ends.
    const text = describeInsert({ ...base, inserted: 1, dropped: ["start", "end"] })

    expect(text).toContain("start·end")
    expect(text).toContain("제외했습니다")
  })

  it("names the renames, because they rewrote someone's references", () => {
    const text = describeInsert({ ...base, inserted: 2, renamed: { llm_1: "llm_3" } })

    expect(text).toContain("llm_1→llm_3")
  })

  it("summarises a long list of renames rather than printing all of them", () => {
    const renamed = { a: "a2", b: "b2", c: "c2", d: "d2", e: "e2" }
    const text = describeInsert({ ...base, inserted: 5, renamed })

    expect(text).toContain("외 2개")
    expect(text).not.toContain("e→e2")
  })

  it("always says undo is available", () => {
    expect(describeInsert({ ...base, inserted: 1 })).toContain("되돌리기")
  })

  it("explains a file that had nothing to place", () => {
    const text = describeInsert({ ...base, inserted: 0, dropped: ["start", "end"] })

    expect(text).toContain("시작·끝 노드만")
    expect(text).not.toContain("되돌리기")
  })
})
