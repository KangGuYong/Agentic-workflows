import { describe, expect, it } from "vitest"

import type { NodeType } from "@/lib/palette"

import { buildUiSchema, SCHEMA_FIELDS, type FieldUi } from "./uiSchema"

/** `ui:order` shares the index signature with the field entries, so a field read narrows here. */
function field(ui: ReturnType<typeof buildUiSchema>, name: string): FieldUi | undefined {
  const value = ui[name]
  return Array.isArray(value) ? undefined : value
}

function nodeType(type: string, properties: Record<string, unknown>): NodeType {
  return {
    type,
    label: type,
    category: "AI",
    isBranch: false,
    sideEffects: false,
    configSchema: { type: "object", properties },
    defaultPolicy: null,
  }
}

describe("buildUiSchema", () => {
  it("routes every x-template property to the template widget", () => {
    // Derived from the schema, never hand-listed: the engine marks these with `x-template`, and a
    // hand-kept list is exactly the kind of thing that drifts the moment a node type gains a field.
    const ui = buildUiSchema(
      nodeType("llm", {
        model: { type: "string" },
        prompt: { type: "string", "x-template": true },
        system: { type: "string", "x-template": true },
      }),
    )

    expect(field(ui, "prompt")?.["ui:widget"]).toBe("template")
    expect(field(ui, "system")?.["ui:widget"]).toBe("template")
    expect(field(ui, "model")?.["ui:widget"]).toBeUndefined()
  })

  it("routes the schema-shaped properties to the schema widget", () => {
    const start = buildUiSchema(nodeType("start", { inputs: { type: "object" } }))
    const llm = buildUiSchema(nodeType("llm", { outputSchema: { type: "object" } }))

    expect(field(start, "inputs")?.["ui:widget"]).toBe("jsonSchema")
    expect(field(llm, "outputSchema")?.["ui:widget"]).toBe("jsonSchema")
  })

  it("only treats a schema field as one on the node type that owns it", () => {
    // `SCHEMA_FIELDS` is keyed by node type for a reason: a future node with a property called
    // `inputs` that is not a JSON Schema must not get the schema editor.
    const other = buildUiSchema(nodeType("merge", { inputs: { type: "string" } }))

    expect(field(other, "inputs")?.["ui:widget"]).toBeUndefined()
    expect(Object.keys(SCHEMA_FIELDS)).toContain("start")
  })

  it("gives known properties Korean titles", () => {
    const ui = buildUiSchema(nodeType("llm", { model: { type: "string" }, temperature: { type: "number" } }))

    expect(field(ui, "model")?.["ui:title"]).toBe("모델")
    expect(field(ui, "temperature")?.["ui:title"]).toBe("창의성")
  })

  it("leaves an unknown property without a title rather than inventing one", () => {
    // RJSF falls back to the schema's own title or the property name. A made-up Korean label for a
    // field the editor has never seen would be worse than the English name.
    const ui = buildUiSchema(nodeType("future", { somethingNew: { type: "string" } }))

    expect(field(ui, "somethingNew")?.["ui:title"]).toBeUndefined()
  })

  it("orders the fields a person fills first at the top", () => {
    const ui = buildUiSchema(
      nodeType("llm", {
        temperature: { type: "number" },
        prompt: { type: "string", "x-template": true },
        model: { type: "string" },
      }),
    )

    expect(ui["ui:order"]).toEqual(["model", "system", "prompt", "temperature", "outputSchema", "*"])
  })

  it("always ends the order with the wildcard, so a new field still renders", () => {
    // Without `*` RJSF throws on any property the order does not mention -- which would turn an engine
    // adding a config field into a blank panel.
    for (const type of ["llm", "http_request", "condition", "future"]) {
      expect(buildUiSchema(nodeType(type, {})) ["ui:order"]?.at(-1)).toBe("*")
    }
  })
})
