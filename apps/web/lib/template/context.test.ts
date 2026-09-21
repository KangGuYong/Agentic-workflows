import { describe, expect, it } from "vitest"

import type { EditorDsl } from "@/lib/dsl/document"
import type { NodeType } from "@/lib/palette"

import { templateContext, type NodeAnalysis } from "./context"

const TYPES: NodeType[] = [
  { type: "llm", label: "LLM", category: "AI", isBranch: false, sideEffects: false, configSchema: {}, defaultPolicy: null },
]

const DSL: EditorDsl = {
  version: "1",
  nodes: [
    { id: "start", type: "start", position: { x: 0, y: 0 } },
    { id: "llm_1", type: "llm", label: "요약", position: { x: 0, y: 0 } },
    { id: "llm_2", type: "llm", position: { x: 0, y: 0 } },
  ],
  edges: [],
}

const ANALYSIS: Record<string, NodeAnalysis> = {
  llm_1: { variables: ["start"], outputSchema: { type: "object", properties: { text: {} } }, handles: [] },
  llm_2: { variables: ["start", "llm_1"], outputSchema: {}, handles: [] },
}

describe("templateContext", () => {
  it("takes labels from the document, not the engine", () => {
    // A rename has to show in the chip immediately. Waiting for `/validate` to answer would make
    // renaming a node feel like it did not work.
    const context = templateContext(DSL, TYPES, ANALYSIS, "llm_2")

    expect(context.nodes.find((node) => node.id === "llm_1")?.label).toBe("요약")
  })

  it("falls back to the node type's label, then the type name", () => {
    const context = templateContext(DSL, TYPES, ANALYSIS, "llm_2")

    expect(context.nodes.find((node) => node.id === "llm_2")?.label).toBe("LLM")
    expect(context.nodes.find((node) => node.id === "start")?.label).toBe("start")
  })

  it("takes the guaranteed set for the node being edited, not any other", () => {
    expect(templateContext(DSL, TYPES, ANALYSIS, "llm_1").guaranteed).toEqual(["start"])
    expect(templateContext(DSL, TYPES, ANALYSIS, "llm_2").guaranteed).toEqual(["start", "llm_1"])
  })

  it("is null, not empty, before validation has answered", () => {
    // Empty would mean "the engine guarantees nothing" and mark every candidate 기본값 필요.
    expect(templateContext(DSL, TYPES, null, "llm_1").guaranteed).toBeNull()
    expect(templateContext(DSL, TYPES, ANALYSIS, "unplaced").guaranteed).toBeNull()
  })

  it("takes output schemas from the engine, unknown where it has none", () => {
    const context = templateContext(DSL, TYPES, ANALYSIS, "llm_2")

    expect(context.nodes.find((node) => node.id === "llm_1")?.outputSchema).toEqual({
      type: "object",
      properties: { text: {} },
    })
    expect(context.nodes.find((node) => node.id === "start")?.outputSchema).toEqual({})
  })

  it("offers every node in the document, including the one being edited", () => {
    // A node may reference its own previous iteration inside a loop; the engine allows it with a
    // `default(...)`, so the editor must not hide it.
    expect(templateContext(DSL, TYPES, ANALYSIS, "llm_1").nodes.map((node) => node.id)).toEqual([
      "start",
      "llm_1",
      "llm_2",
    ])
  })
})
