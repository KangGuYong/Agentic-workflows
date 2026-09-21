import { describe, expect, it } from "vitest"

import { CATEGORY_ORDER, groupByCategory, type NodeType } from "./palette"

function type(name: string, category: string): NodeType {
  return { type: name, label: name, category, isBranch: false, sideEffects: false, configSchema: {}, defaultPolicy: null }
}

/** The nine types the engine registers, in the order `/node-types` returns them. */
function registry(): NodeType[] {
  return [
    type("start", "IO"),
    type("end", "IO"),
    type("template", "Logic"),
    type("llm", "AI"),
    type("classifier", "AI"),
    type("condition", "Logic"),
    type("merge", "Logic"),
    type("human_approval", "Human"),
    type("http_request", "Action"),
  ]
}

describe("groupByCategory", () => {
  it("returns the palette's fixed order, not the API's", () => {
    // `/node-types` order follows the registry, which is an engine implementation detail. The palette's
    // order is a design decision: what you reach for first is at the top.
    const groups = groupByCategory(registry())

    expect(groups.map((group) => group.category)).toEqual([...CATEGORY_ORDER])
  })

  it("keeps every type, and each within its own group", () => {
    const groups = groupByCategory(registry())

    expect(groups.flatMap((group) => group.types).map((item) => item.type)).toHaveLength(9)
    expect(groups.find((group) => group.category === "AI")?.types.map((item) => item.type)).toEqual([
      "llm",
      "classifier",
    ])
    expect(groups.find((group) => group.category === "Action")?.types.map((item) => item.type)).toEqual([
      "http_request",
    ])
  })

  it("preserves the API's order within a group", () => {
    // Nothing better to sort by: the engine has no ordering hint, and alphabetising by an English type
    // name would be meaningless next to Korean labels.
    const groups = groupByCategory(registry())

    expect(groups.find((group) => group.category === "Logic")?.types.map((item) => item.type)).toEqual([
      "template",
      "condition",
      "merge",
    ])
  })

  it("puts an unknown category last, under 기타, rather than dropping it", () => {
    // A node type the editor has never heard of must still be placeable. Silently dropping it would
    // make a workflow unbuildable with no visible reason.
    const groups = groupByCategory([...registry(), type("future_thing", "Quantum")])

    const last = groups.at(-1)
    expect(last?.category).toBe("기타")
    expect(last?.types.map((item) => item.type)).toEqual(["future_thing"])
  })

  it("collects every unknown category into the one 기타 group", () => {
    const groups = groupByCategory([type("a", "X"), type("b", "Y")])

    expect(groups).toHaveLength(1)
    expect(groups[0]?.types.map((item) => item.type)).toEqual(["a", "b"])
  })

  it("omits a group with nothing in it", () => {
    // An empty heading is noise, and the palette is the first thing a new user reads.
    const groups = groupByCategory([type("llm", "AI")])

    expect(groups.map((group) => group.category)).toEqual(["AI"])
  })

  it("handles an empty registry without inventing groups", () => {
    expect(groupByCategory([])).toEqual([])
  })
})
