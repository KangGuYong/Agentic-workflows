import { describe, expect, it } from "vitest"

import type { EditorDsl } from "./document"
import { NODE_SIZE, positionsFrom, toElkGraph, type ElkResult } from "./layout"

function chain(): EditorDsl {
  return {
    version: "1",
    nodes: [
      { id: "start", type: "start", position: { x: 500, y: 300 } },
      { id: "llm_1", type: "llm", position: { x: 20, y: 900 } },
      { id: "end", type: "end", position: { x: 0, y: 0 } },
    ],
    edges: [
      { id: "e1", source: "start", target: "llm_1" },
      { id: "e2", source: "llm_1", target: "end" },
    ],
  }
}

describe("toElkGraph", () => {
  it("gives every node a size, since ELK cannot measure the DOM", () => {
    const graph = toElkGraph(chain())

    expect(graph.children).toHaveLength(3)
    for (const child of graph.children) {
      expect(child).toMatchObject({ width: NODE_SIZE.width, height: NODE_SIZE.height })
    }
  })

  it("lays out left to right", () => {
    expect(toElkGraph(chain()).layoutOptions["elk.direction"]).toBe("RIGHT")
  })

  it("carries every edge", () => {
    const graph = toElkGraph(chain())

    expect(graph.edges).toEqual([
      { id: "e1", sources: ["start"], targets: ["llm_1"] },
      { id: "e2", sources: ["llm_1"], targets: ["end"] },
    ])
  })

  it("drops an edge whose endpoints are not both present", () => {
    // ELK throws on an edge pointing at a node it does not know. A document should never hold one --
    // `removeNode` clears them -- but an imported or hand-edited one can, and auto-layout crashing is a
    // worse answer than laying out what it can.
    const broken = chain()
    broken.edges.push({ id: "e9", source: "llm_1", target: "ghost" })

    expect(toElkGraph(broken).edges.map((edge) => edge.id)).toEqual(["e1", "e2"])
  })
})

describe("positionsFrom", () => {
  const result: ElkResult = {
    children: [
      { id: "start", x: 0, y: 40 },
      { id: "llm_1", x: 220, y: 40 },
      { id: "end", x: 440, y: 40 },
    ],
  }

  it("reads each node's placement", () => {
    expect(positionsFrom(result)).toEqual({
      start: { x: 0, y: 40 },
      llm_1: { x: 220, y: 40 },
      end: { x: 440, y: 40 },
    })
  })

  it("skips a child ELK placed nowhere", () => {
    // Defensive: ELK's typings make x and y optional, and a node it could not place would otherwise
    // become `{x: undefined, y: undefined}` and vanish from the canvas.
    expect(positionsFrom({ children: [{ id: "a" }, { id: "b", x: 1, y: 2 }] })).toEqual({
      b: { x: 1, y: 2 },
    })
  })

  it("survives a result with no children", () => {
    expect(positionsFrom({})).toEqual({})
  })
})

describe("the two together", () => {
  it("describes a chain ELK will lay out in increasing x", () => {
    // The real ordering assertion lives in the store test, which runs ELK for real. This one only
    // pins that the graph handed to ELK says what it needs to say for that to be possible.
    const graph = toElkGraph(chain())

    expect(graph.layoutOptions["elk.algorithm"]).toBe("layered")
    expect(graph.children.map((child) => child.id)).toEqual(["start", "llm_1", "end"])
  })
})
