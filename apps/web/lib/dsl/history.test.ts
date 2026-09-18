import { describe, expect, it } from "vitest"

import { addNode, setLabel, setPositions } from "./commands"
import { emptyDsl, type EditorDsl } from "./document"
import { MAX_HISTORY, apply, canRedo, canUndo, commit, createHistory, redo, undo } from "./history"

function ids(dsl: EditorDsl): string[] {
  return dsl.nodes.map((node) => node.id)
}

/** Three nodes added one at a time, so each step is a distinct snapshot. */
function three() {
  let history = createHistory(emptyDsl())
  for (const type of ["llm", "template", "merge"]) {
    history = apply(history, addNode(history.present, type, { x: 0, y: 0 }))
  }
  return history
}

describe("undo and redo", () => {
  it("walks back and forward through the states a command produced", () => {
    let history = three()
    expect(ids(history.present)).toEqual(["llm_1", "template_1", "merge_1"])

    history = undo(undo(history))
    expect(ids(history.present)).toEqual(["llm_1"])

    history = redo(history)
    expect(ids(history.present)).toEqual(["llm_1", "template_1"])
  })

  it("does nothing at either end rather than throwing", () => {
    const fresh = createHistory(emptyDsl())
    expect(canUndo(fresh)).toBe(false)
    expect(undo(fresh)).toBe(fresh)

    const one = apply(fresh, addNode(fresh.present, "llm", { x: 0, y: 0 }))
    expect(canRedo(one)).toBe(false)
    expect(redo(one)).toBe(one)
  })

  it("clears the redo stack when a new command follows an undo", () => {
    let history = undo(three())
    expect(canRedo(history)).toBe(true)

    history = apply(history, addNode(history.present, "condition", { x: 0, y: 0 }))

    expect(canRedo(history)).toBe(false)
    expect(ids(history.present)).toEqual(["llm_1", "template_1", "condition_1"])
  })

  it("ignores a command that changed nothing", () => {
    // Commands return the document unchanged, by reference, when they cannot do anything. Pushing that
    // would spend an undo step on a no-op -- the user presses undo and nothing appears to happen.
    const history = three()
    expect(apply(history, history.present)).toBe(history)
  })
})

describe("the history bound", () => {
  it("keeps the most recent MAX_HISTORY steps and drops the oldest", () => {
    let history = createHistory(emptyDsl())
    for (let index = 0; index < MAX_HISTORY + 10; index += 1) {
      history = apply(history, addNode(history.present, "llm", { x: 0, y: 0 }))
    }
    expect(history.past).toHaveLength(MAX_HISTORY)

    for (let index = 0; index < MAX_HISTORY; index += 1) history = undo(history)

    expect(canUndo(history)).toBe(false)
    // 10 of the 60 nodes are beyond the window, so the oldest reachable state still has them.
    expect(history.present.nodes).toHaveLength(10)
  })
})

describe("merging a drag", () => {
  const DRAG = "position:llm_1"

  it("collapses consecutive moves of the same node into one undo step", () => {
    let history = createHistory(addNode(emptyDsl(), "llm", { x: 0, y: 0 }))
    for (const x of [10, 20, 30, 40]) {
      history = apply(history, setPositions(history.present, { llm_1: { x, y: 0 } }), { mergeKey: DRAG })
    }

    expect(history.past).toHaveLength(1)
    expect(history.present.nodes[0]?.position).toEqual({ x: 40, y: 0 })
  })

  it("undoes a merged drag to before the drag, not to an intermediate frame", () => {
    // This is the direction that is easy to get backwards: a merged push must keep the *older* snapshot,
    // because that is the state undo has to return to. Replacing the top of the stack with each frame
    // looks right until you undo, and then the node jumps to wherever the drag last passed through.
    let history = createHistory(addNode(emptyDsl(), "llm", { x: 5, y: 5 }))
    for (const x of [10, 20, 30]) {
      history = apply(history, setPositions(history.present, { llm_1: { x, y: 5 } }), { mergeKey: DRAG })
    }

    history = undo(history)

    expect(history.present.nodes[0]?.position).toEqual({ x: 5, y: 5 })
  })

  it("does not merge moves of different nodes", () => {
    let history = createHistory(addNode(addNode(emptyDsl(), "llm", { x: 0, y: 0 }), "template", { x: 0, y: 0 }))
    history = apply(history, setPositions(history.present, { llm_1: { x: 1, y: 0 } }), { mergeKey: "position:llm_1" })
    history = apply(history, setPositions(history.present, { template_1: { x: 2, y: 0 } }), {
      mergeKey: "position:template_1",
    })

    expect(history.past).toHaveLength(2)
  })

  it("does not merge across a command with no merge key", () => {
    let history = createHistory(addNode(emptyDsl(), "llm", { x: 0, y: 0 }))
    history = apply(history, setPositions(history.present, { llm_1: { x: 1, y: 0 } }), { mergeKey: DRAG })
    history = apply(history, setLabel(history.present, "llm_1", "요약"))
    history = apply(history, setPositions(history.present, { llm_1: { x: 2, y: 0 } }), { mergeKey: DRAG })

    expect(history.past).toHaveLength(3)
  })

  it("commit() ends the window so the next move starts a new step", () => {
    // What the canvas calls when a drag ends: without it, picking the same node up again would merge
    // into the previous drag and both would undo together.
    let history = createHistory(addNode(emptyDsl(), "llm", { x: 0, y: 0 }))
    history = apply(history, setPositions(history.present, { llm_1: { x: 1, y: 0 } }), { mergeKey: DRAG })
    history = commit(history)
    history = apply(history, setPositions(history.present, { llm_1: { x: 2, y: 0 } }), { mergeKey: DRAG })

    expect(history.past).toHaveLength(2)
  })

  it("undo ends the window too", () => {
    // Otherwise a move after an undo would merge into the step the undo just returned to, and the two
    // would collapse into one.
    let history = createHistory(addNode(emptyDsl(), "llm", { x: 0, y: 0 }))
    history = apply(history, setPositions(history.present, { llm_1: { x: 1, y: 0 } }), { mergeKey: DRAG })
    history = undo(history)
    history = apply(history, setPositions(history.present, { llm_1: { x: 9, y: 0 } }), { mergeKey: DRAG })

    expect(history.past).toHaveLength(1)
    expect(undo(history).present.nodes[0]?.position).toEqual({ x: 0, y: 0 })
  })
})

describe("purity", () => {
  it("never mutates the history it is given", () => {
    const before = three()
    const pastLength = before.past.length
    const present = before.present

    apply(before, addNode(before.present, "llm", { x: 0, y: 0 }))
    undo(before)
    commit(before)

    expect(before.past).toHaveLength(pastLength)
    expect(before.present).toBe(present)
  })
})
