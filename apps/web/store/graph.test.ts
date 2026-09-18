import { beforeEach, describe, expect, it } from "vitest"

import { emptyDsl, type EditorDsl } from "@/lib/dsl/document"

import { createGraphStore, type GraphStore } from "./graph"

let store: GraphStore

function dsl(): EditorDsl {
  return store.getState().dsl
}

function ids(): string[] {
  return dsl().nodes.map((node) => node.id)
}

beforeEach(() => {
  store = createGraphStore(emptyDsl())
})

describe("placing nodes", () => {
  it("adds a node where it was dropped", () => {
    store.getState().addNodeAt("llm", { x: 40, y: 50 })

    expect(ids()).toEqual(["llm_1"])
    expect(dsl().nodes[0]?.position).toEqual({ x: 40, y: 50 })
  })

  it("selects what it just added, so the panel opens on it", () => {
    store.getState().addNodeAt("llm", { x: 0, y: 0 })

    expect(store.getState().selection).toEqual({ nodes: ["llm_1"], edges: [] })
  })

  it("reports a refused node type instead of throwing into the drop handler", () => {
    // A second `start` is the one the engine forbids. The canvas cannot let an exception escape a drag
    // event, so the store turns it into a message the UI can show.
    store.getState().addNodeAt("start", { x: 0, y: 0 })
    store.getState().addNodeAt("start", { x: 10, y: 10 })

    expect(ids()).toEqual(["start"])
    expect(store.getState().lastError).toMatch(/start/)
  })

  it("clears the last error on the next successful edit", () => {
    store.getState().addNodeAt("start", { x: 0, y: 0 })
    store.getState().addNodeAt("start", { x: 0, y: 0 })
    store.getState().addNodeAt("llm", { x: 0, y: 0 })

    expect(store.getState().lastError).toBeNull()
  })
})

describe("connecting", () => {
  beforeEach(() => {
    store.getState().addNodeAt("start", { x: 0, y: 0 })
    store.getState().addNodeAt("llm", { x: 100, y: 0 })
  })

  it("adds an edge between two nodes", () => {
    store.getState().connectNodes({ source: "start", sourceHandle: "out", target: "llm_1" })

    expect(dsl().edges).toHaveLength(1)
  })

  it("spends no undo step on a connection the command refuses", () => {
    store.getState().connectNodes({ source: "start", sourceHandle: "out", target: "llm_1" })
    const depth = store.getState().history.past.length

    store.getState().connectNodes({ source: "start", sourceHandle: "out", target: "llm_1" })

    expect(dsl().edges).toHaveLength(1)
    expect(store.getState().history.past).toHaveLength(depth)
  })
})

describe("deleting the selection", () => {
  beforeEach(() => {
    store.getState().addNodeAt("start", { x: 0, y: 0 })
    store.getState().addNodeAt("llm", { x: 100, y: 0 })
    store.getState().addNodeAt("end", { x: 200, y: 0 })
    store.getState().connectNodes({ source: "start", sourceHandle: "out", target: "llm_1" })
    store.getState().connectNodes({ source: "llm_1", sourceHandle: "out", target: "end" })
  })

  it("removes a node and both of its edges", () => {
    store.getState().select({ nodes: ["llm_1"], edges: [] })
    store.getState().removeSelected()

    expect(ids()).toEqual(["start", "end"])
    expect(dsl().edges).toEqual([])
  })

  it("removes selected edges without touching their nodes", () => {
    store.getState().select({ nodes: [], edges: ["e1"] })
    store.getState().removeSelected()

    expect(ids()).toHaveLength(3)
    expect(dsl().edges.map((edge) => edge.id)).toEqual(["e2"])
  })

  it("undoes a multi-node delete in one step", () => {
    // Everything the user selected goes in one command, so one undo brings it all back. Deleting node
    // by node would make undo a game of how many times to press it.
    store.getState().select({ nodes: ["start", "llm_1"], edges: [] })
    store.getState().removeSelected()
    expect(ids()).toEqual(["end"])

    store.getState().undo()

    expect(ids()).toEqual(["start", "llm_1", "end"])
    expect(dsl().edges).toHaveLength(2)
  })

  it("clears the selection, since what was selected is gone", () => {
    store.getState().select({ nodes: ["llm_1"], edges: [] })
    store.getState().removeSelected()

    expect(store.getState().selection).toEqual({ nodes: [], edges: [] })
  })

  it("does nothing when nothing is selected", () => {
    // Explicitly cleared first: `addNodeAt` leaves the node it placed selected, so a Delete press right
    // after dropping one removes it -- which is what a canvas should do, and why this test has to say
    // "nothing selected" rather than assume it.
    store.getState().select({ nodes: [], edges: [] })
    const depth = store.getState().history.past.length

    store.getState().removeSelected()

    expect(ids()).toHaveLength(3)
    expect(store.getState().history.past).toHaveLength(depth)
  })

  it("deletes a just-placed node on the next Delete, since placing selects it", () => {
    store.getState().addNodeAt("merge", { x: 0, y: 0 })
    expect(store.getState().selection.nodes).toEqual(["merge_1"])

    store.getState().removeSelected()

    expect(ids()).not.toContain("merge_1")
  })
})

describe("dragging", () => {
  beforeEach(() => {
    store.getState().addNodeAt("llm", { x: 0, y: 0 })
  })

  it("collapses a whole drag into one undo step", () => {
    const depth = store.getState().history.past.length
    for (const x of [1, 2, 3, 4, 5]) {
      store.getState().moveNodes([{ id: "llm_1", type: "position", position: { x, y: 0 }, dragging: true }])
    }

    expect(store.getState().history.past).toHaveLength(depth + 1)
    expect(dsl().nodes[0]?.position).toEqual({ x: 5, y: 0 })
  })

  it("undoes a drag to where it started", () => {
    store.getState().moveNodes([{ id: "llm_1", type: "position", position: { x: 9, y: 9 }, dragging: true }])
    store.getState().endDrag()

    store.getState().undo()

    expect(dsl().nodes[0]?.position).toEqual({ x: 0, y: 0 })
  })

  it("starts a new step after the drag ends", () => {
    store.getState().moveNodes([{ id: "llm_1", type: "position", position: { x: 1, y: 0 }, dragging: true }])
    store.getState().endDrag()
    const depth = store.getState().history.past.length

    store.getState().moveNodes([{ id: "llm_1", type: "position", position: { x: 2, y: 0 }, dragging: true }])

    expect(store.getState().history.past).toHaveLength(depth + 1)
  })

  it("merges a multi-node drag as one step, keyed on the whole set", () => {
    store.getState().addNodeAt("template", { x: 0, y: 0 })
    const depth = store.getState().history.past.length

    for (const x of [1, 2]) {
      store.getState().moveNodes([
        { id: "llm_1", type: "position", position: { x, y: 0 }, dragging: true },
        { id: "template_1", type: "position", position: { x, y: 50 }, dragging: true },
      ])
    }

    expect(store.getState().history.past).toHaveLength(depth + 1)
  })

  it("ignores a change list with no moves in it", () => {
    const depth = store.getState().history.past.length
    store.getState().moveNodes([{ id: "llm_1", type: "select", selected: true }])

    expect(store.getState().history.past).toHaveLength(depth)
  })
})

describe("undo and redo", () => {
  it("are exposed with their availability, so the toolbar can disable them", () => {
    expect(store.getState().canUndo).toBe(false)
    expect(store.getState().canRedo).toBe(false)

    store.getState().addNodeAt("llm", { x: 0, y: 0 })
    expect(store.getState().canUndo).toBe(true)

    store.getState().undo()
    expect(store.getState().canRedo).toBe(true)
    expect(ids()).toEqual([])
  })

  it("clears the selection, which may point at a node that no longer exists", () => {
    store.getState().addNodeAt("llm", { x: 0, y: 0 })
    store.getState().undo()

    expect(store.getState().selection.nodes).toEqual([])
  })
})

describe("auto layout", () => {
  it("places a chain left to right and keeps every node", async () => {
    // Runs ELK for real: the mapping tests pin what it is told and how its answer is read, but only
    // this proves the two fit together and that the options produce the direction we asked for.
    store.getState().addNodeAt("start", { x: 900, y: 40 })
    store.getState().addNodeAt("llm", { x: 10, y: 700 })
    store.getState().addNodeAt("end", { x: 400, y: 300 })
    store.getState().connectNodes({ source: "start", sourceHandle: "out", target: "llm_1" })
    store.getState().connectNodes({ source: "llm_1", sourceHandle: "out", target: "end" })

    await store.getState().autoLayout()

    const x = dsl().nodes.map((node) => node.position.x)
    expect(ids()).toEqual(["start", "llm_1", "end"])
    expect(x[0]).toBeLessThan(x[1] as number)
    expect(x[1]).toBeLessThan(x[2] as number)
  })

  it("undoes the whole layout in one step", async () => {
    store.getState().addNodeAt("start", { x: 900, y: 40 })
    store.getState().addNodeAt("llm", { x: 10, y: 700 })
    store.getState().connectNodes({ source: "start", sourceHandle: "out", target: "llm_1" })
    const before = dsl().nodes.map((node) => ({ ...node.position }))

    await store.getState().autoLayout()
    store.getState().undo()

    expect(dsl().nodes.map((node) => node.position)).toEqual(before)
  })

  it("spends no undo step on an empty document", async () => {
    const depth = store.getState().history.past.length

    await store.getState().autoLayout()

    expect(store.getState().history.past).toHaveLength(depth)
  })

  it("refuses to run twice at once", async () => {
    // A double click must not lay out twice. Asserting on `layingOut` alone would not catch that: it is
    // false at the end either way. The undo depth is what tells them apart -- two layouts are two
    // commands, and the user would have to press undo twice to get back.
    store.getState().addNodeAt("start", { x: 900, y: 40 })
    store.getState().addNodeAt("llm", { x: 10, y: 700 })
    store.getState().connectNodes({ source: "start", sourceHandle: "out", target: "llm_1" })
    const depth = store.getState().history.past.length

    await Promise.all([store.getState().autoLayout(), store.getState().autoLayout()])

    expect(store.getState().history.past).toHaveLength(depth + 1)
    expect(store.getState().layingOut).toBe(false)
  })
})
