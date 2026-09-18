import { describe, expect, it } from "vitest"

import { movedPositions, toFlowEdges, toFlowNodes } from "./flow"
import type { EditorDsl } from "./document"

const DSL: EditorDsl = {
  version: "1",
  nodes: [
    { id: "start", type: "start", position: { x: 0, y: 0 } },
    { id: "condition_1", type: "condition", label: "점수 확인", position: { x: 100, y: 0 } },
    { id: "end", type: "end", position: { x: 200, y: 0 } },
  ],
  edges: [
    { id: "e1", source: "start", target: "condition_1" },
    { id: "e2", source: "condition_1", sourceHandle: "true", target: "end" },
  ],
}

describe("toFlowNodes", () => {
  it("carries the id, position and label the canvas draws", () => {
    const nodes = toFlowNodes(DSL, {})

    expect(nodes[1]).toMatchObject({
      id: "condition_1",
      position: { x: 100, y: 0 },
      data: { label: "점수 확인", type: "condition" },
    })
  })

  it("falls back to the node type's label when the node has none", () => {
    const nodes = toFlowNodes(DSL, { labels: { start: "시작" } })

    expect(nodes[0]?.data.label).toBe("시작")
  })

  it("falls back to the raw type when even that is unknown", () => {
    // A node type the editor has not heard of still has to draw something readable.
    expect(toFlowNodes(DSL, {})[0]?.data.label).toBe("start")
  })

  it("uses the handles the last analysis reported", () => {
    const nodes = toFlowNodes(DSL, { handles: { condition_1: ["true", "false"] } })

    expect(nodes[1]?.data.handles).toEqual(["true", "false"])
  })

  it("falls back to a single default handle before the first analysis", () => {
    // A node dropped a moment ago has no analysis yet -- and never will while it is unconnected, since
    // HANDLE_NOT_CONNECTED is a phase-2 error that leaves `nodes` empty. It still needs a handle to
    // drag a connection out of, or it can never be connected at all.
    expect(toFlowNodes(DSL, {})[1]?.data.handles).toEqual(["out"])
  })
})

describe("toFlowEdges", () => {
  it("makes the default handle explicit, because React Flow matches on it", () => {
    // The DSL omits `sourceHandle: "out"` so that two documents meaning the same thing hash the same.
    // React Flow has no such rule: an edge whose sourceHandle is undefined will not attach to a Handle
    // rendered with id "out", and the edge silently draws from the node's centre instead.
    const edges = toFlowEdges(DSL)

    expect(edges[0]).toMatchObject({ id: "e1", source: "start", target: "condition_1", sourceHandle: "out" })
    expect(edges[1]?.sourceHandle).toBe("true")
  })
})

describe("movedPositions", () => {
  it("collects position changes by node id", () => {
    const moved = movedPositions([
      { id: "start", type: "position", position: { x: 5, y: 6 }, dragging: true },
      { id: "end", type: "position", position: { x: 7, y: 8 }, dragging: true },
    ])

    expect(moved).toEqual({ start: { x: 5, y: 6 }, end: { x: 7, y: 8 } })
  })

  it("ignores changes that are not moves", () => {
    const moved = movedPositions([
      { id: "start", type: "select", selected: true },
      { id: "end", type: "position", position: { x: 1, y: 2 }, dragging: true },
      { id: "x", type: "remove" },
    ])

    expect(moved).toEqual({ end: { x: 1, y: 2 } })
  })

  it("ignores a position change that carries no position", () => {
    // React Flow emits `{type: "position", dragging: false}` with no position when a drag ends. Reading
    // it as a move would write `undefined` over the node's real coordinates.
    expect(movedPositions([{ id: "start", type: "position", dragging: false }])).toEqual({})
  })
})
