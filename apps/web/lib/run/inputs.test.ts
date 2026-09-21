import { describe, expect, it } from "vitest"

import { emptyDsl, type EditorDsl } from "@/lib/dsl/document"

import { inputSchema, needsInputs } from "./inputs"

function withStart(config: Record<string, unknown> | undefined): EditorDsl {
  return {
    ...emptyDsl(),
    nodes: [
      { id: "start", type: "start", position: { x: 0, y: 0 }, config },
      { id: "llm_1", type: "llm", position: { x: 0, y: 0 }, config: { inputs: { type: "object" } } },
    ],
  }
}

const SCHEMA = { type: "object", properties: { issue: { type: "string" } }, required: ["issue"] }

describe("inputSchema", () => {
  it("is the start node's declared inputs", () => {
    expect(inputSchema(withStart({ inputs: SCHEMA }))).toEqual(SCHEMA)
  })

  it("is null when the workflow declares none", () => {
    expect(inputSchema(withStart({}))).toBeNull()
    expect(inputSchema(withStart(undefined))).toBeNull()
    expect(inputSchema(emptyDsl())).toBeNull()
  })

  it("treats an empty schema as none, because it declares nothing", () => {
    expect(inputSchema(withStart({ inputs: {} }))).toBeNull()
  })

  it("ignores a config value that is not a schema object", () => {
    for (const inputs of ["x", 1, null, []]) {
      expect(inputSchema(withStart({ inputs })), JSON.stringify(inputs)).toBeNull()
    }
  })

  it("reads the start node, not some other node that happens to have an `inputs` config", () => {
    // `llm_1` in the fixture has one too. The engine reserves the id `start`, so there is no ambiguity
    // to resolve by node type.
    expect(inputSchema(withStart({ inputs: SCHEMA }))).toEqual(SCHEMA)
  })
})

describe("needsInputs", () => {
  it("is true when the schema declares a property", () => {
    expect(needsInputs(withStart({ inputs: SCHEMA }))).toBe(true)
  })

  it("is false when it declares none", () => {
    // A dialog with no fields is a dialog asking nothing; the run should just start.
    expect(needsInputs(withStart({ inputs: { type: "object", properties: {} } }))).toBe(false)
    expect(needsInputs(withStart({}))).toBe(false)
  })
})
