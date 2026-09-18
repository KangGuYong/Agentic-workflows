import { describe, expect, it } from "vitest"

import {
  addNode,
  connect,
  disconnect,
  removeNode,
  setConfig,
  setLabel,
  setPolicy,
  setPositions,
} from "./commands"
import { emptyDsl, type EditorDsl } from "./document"

/** Deep-frozen, so any command that mutates its input throws instead of passing quietly. */
function frozen(dsl: EditorDsl): EditorDsl {
  for (const node of dsl.nodes) {
    if (node.config !== undefined) Object.freeze(node.config)
    Object.freeze(node.position)
    Object.freeze(node)
  }
  for (const edge of dsl.edges) Object.freeze(edge)
  Object.freeze(dsl.nodes)
  Object.freeze(dsl.edges)
  return Object.freeze(dsl)
}

function chain(): EditorDsl {
  return frozen({
    version: "1",
    nodes: [
      { id: "start", type: "start", position: { x: 0, y: 0 } },
      { id: "llm_1", type: "llm", config: { model: "m", prompt: "p" }, position: { x: 100, y: 0 } },
      { id: "end", type: "end", position: { x: 200, y: 0 } },
    ],
    edges: [
      { id: "e1", source: "start", target: "llm_1" },
      { id: "e2", source: "llm_1", target: "end" },
    ],
  })
}

describe("every command", () => {
  it("returns a new document and leaves the old one alone", () => {
    // Frozen input: a command that mutates throws in strict mode, which every ES module is. This is the
    // property undo/redo depends on -- the history keeps snapshots, so a shared mutable node would let
    // an edit reach back and change the past.
    const before = chain()

    const after = [
      addNode(before, "template", { x: 10, y: 10 }),
      removeNode(before, "llm_1"),
      connect(before, { source: "start", target: "end" }),
      disconnect(before, "e1"),
      setConfig(before, "llm_1", { model: "other" }),
      setLabel(before, "llm_1", "요약"),
      setPolicy(before, "llm_1", { timeoutSec: 5 }),
      setPositions(before, { llm_1: { x: 9, y: 9 } }),
    ]

    for (const result of after) expect(result).not.toBe(before)
    expect(before.nodes.map((node) => node.id)).toEqual(["start", "llm_1", "end"])
    expect(before.edges).toHaveLength(2)
  })
})

describe("addNode", () => {
  it("places a node with a generated id and returns it", () => {
    const result = addNode(emptyDsl(), "llm", { x: 5, y: 6 })

    expect(result.nodes).toHaveLength(1)
    expect(result.nodes[0]).toMatchObject({ id: "llm_1", type: "llm", position: { x: 5, y: 6 } })
  })

  it("refuses a second start or end", () => {
    // The engine allows exactly one of each, and a second would take the same id as the first.
    expect(() => addNode(chain(), "start", { x: 0, y: 0 })).toThrow(/start/)
    expect(() => addNode(chain(), "end", { x: 0, y: 0 })).toThrow(/end/)
  })
})

describe("removeNode", () => {
  it("removes every edge touching the node, incoming and outgoing", () => {
    const result = removeNode(chain(), "llm_1")

    expect(result.nodes.map((node) => node.id)).toEqual(["start", "end"])
    expect(result.edges).toEqual([])
  })

  it("is a no-op for a node that is not there", () => {
    const before = chain()
    expect(removeNode(before, "nope").nodes).toHaveLength(3)
  })
})

describe("connect", () => {
  it("adds an edge with a fresh id", () => {
    const result = connect(chain(), { source: "start", target: "end" })

    expect(result.edges.map((edge) => edge.id)).toEqual(["e1", "e2", "e3"])
    expect(result.edges[2]).toMatchObject({ source: "start", target: "end" })
  })

  it("keeps a sourceHandle when one is given, and omits it otherwise", () => {
    // `sourceHandle` defaults to "out" in the engine. Writing it explicitly everywhere would change
    // `dsl_hash` for documents that mean exactly the same thing.
    const withHandle = connect(chain(), { source: "start", sourceHandle: "true", target: "end" })
    expect(withHandle.edges[2]?.sourceHandle).toBe("true")

    const without = connect(chain(), { source: "start", target: "end" })
    expect(without.edges[2] && "sourceHandle" in without.edges[2]).toBe(false)
  })

  it("refuses a duplicate connection", () => {
    // The engine reports EDGE_DUPLICATE for it; the editor should not create one in the first place.
    const before = chain()
    expect(connect(before, { source: "start", target: "llm_1" })).toBe(before)
  })

  it("treats an explicit default handle as the same connection as an absent one", () => {
    const before = chain()
    expect(connect(before, { source: "start", sourceHandle: "out", target: "llm_1" })).toBe(before)
  })

  it("refuses an edge to or from a node that is not there", () => {
    const before = chain()
    expect(connect(before, { source: "nope", target: "end" })).toBe(before)
    expect(connect(before, { source: "start", target: "nope" })).toBe(before)
  })

  it("refuses a self-loop", () => {
    // The engine's graph rules have no room for one, and React Flow will happily offer it.
    const before = chain()
    expect(connect(before, { source: "llm_1", target: "llm_1" })).toBe(before)
  })
})

describe("setConfig", () => {
  it("replaces the config rather than merging into it", () => {
    // A merge cannot delete a key, so clearing an optional field would be impossible.
    const result = setConfig(chain(), "llm_1", { model: "other" })

    expect(result.nodes[1]?.config).toEqual({ model: "other" })
  })
})

describe("setPolicy", () => {
  it("removes the key entirely when the override is empty", () => {
    // `dsl_hash` hashes `policy: {}` and a missing policy differently even though both mean "no
    // override", so writing an empty object would create a new workflow version that changes nothing.
    const withPolicy = setPolicy(chain(), "llm_1", { timeoutSec: 5 })
    expect(withPolicy.nodes[1]?.policy).toEqual({ timeoutSec: 5 })

    const cleared = setPolicy(withPolicy, "llm_1", {})
    expect(cleared.nodes[1] && "policy" in cleared.nodes[1]).toBe(false)
  })

  it("treats a policy whose fields are all undefined as empty", () => {
    const result = setPolicy(chain(), "llm_1", { timeoutSec: undefined, retry: undefined })

    expect(result.nodes[1] && "policy" in result.nodes[1]).toBe(false)
  })

  it("drops an empty retry object for the same reason", () => {
    const result = setPolicy(chain(), "llm_1", { timeoutSec: 5, retry: {} })

    expect(result.nodes[1]?.policy).toEqual({ timeoutSec: 5 })
  })
})

describe("setLabel", () => {
  it("removes the key when the label is blank, rather than storing an empty string", () => {
    const labelled = setLabel(chain(), "llm_1", "요약")
    expect(labelled.nodes[1]?.label).toBe("요약")

    const cleared = setLabel(labelled, "llm_1", "   ")
    expect(cleared.nodes[1] && "label" in cleared.nodes[1]).toBe(false)
  })
})

describe("setPositions", () => {
  it("moves several nodes at once", () => {
    // One command per drag, not one per node: a multi-select drag must undo as a single step.
    const result = setPositions(chain(), { start: { x: 1, y: 2 }, end: { x: 3, y: 4 } })

    expect(result.nodes[0]?.position).toEqual({ x: 1, y: 2 })
    expect(result.nodes[2]?.position).toEqual({ x: 3, y: 4 })
    expect(result.nodes[1]?.position).toEqual({ x: 100, y: 0 })
  })

  it("ignores a node id it does not know", () => {
    expect(setPositions(chain(), { nope: { x: 1, y: 1 } }).nodes).toHaveLength(3)
  })
})
