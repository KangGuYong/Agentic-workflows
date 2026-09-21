import { describe, expect, it } from "vitest"

import { emptyModel, toModel, toSchema, type SchemaModel } from "./model"

function field(name: string, kind: "string" | "number", required = false, description = "") {
  return { name, required, description, value: { kind } as const }
}

describe("toSchema", () => {
  it("writes the fields, marking the required ones", () => {
    const model: SchemaModel = { description: "", fields: [field("a", "string", true), field("b", "number")] }

    expect(toSchema(model)).toEqual({
      type: "object",
      properties: { a: { type: "string" }, b: { type: "number" } },
      required: ["a"],
    })
  })

  it("omits `required` entirely when nothing is required", () => {
    expect(toSchema({ description: "", fields: [field("a", "string")] })).toEqual({
      type: "object",
      properties: { a: { type: "string" } },
    })
  })

  it("writes an empty object schema, not `{}`, when the last field is removed", () => {
    // `{}` means "any object at all" to the engine. Removing the last row means "an object with no
    // declared fields", which is a different statement and the one the person made.
    expect(toSchema(emptyModel())).toEqual({ type: "object", properties: {} })
  })

  it("writes a description only when there is one", () => {
    expect(toSchema({ description: "", fields: [field("a", "string", false, "이름")] })).toEqual({
      type: "object",
      properties: { a: { type: "string", description: "이름" } },
    })
  })

  it("nests objects and arrays", () => {
    const model: SchemaModel = {
      description: "",
      fields: [
        {
          name: "items",
          required: true,
          description: "",
          value: { kind: "array", item: { kind: "object", fields: [field("id", "string", true)] } },
        },
      ],
    }

    expect(toSchema(model)).toEqual({
      type: "object",
      required: ["items"],
      properties: {
        items: {
          type: "array",
          items: { type: "object", properties: { id: { type: "string" } }, required: ["id"] },
        },
      },
    })
  })
})

describe("toModel", () => {
  it("opens an empty form for a schema that is not there yet", () => {
    expect(toModel(undefined)).toEqual(emptyModel())
    expect(toModel(null)).toEqual(emptyModel())
    expect(toModel({})).toEqual(emptyModel())
  })

  it("carries the root description, which has no field row to live on", () => {
    // Reading it without writing it back was a silent drop, caught by the round-trip test below.
    const model = toModel({ type: "object", description: "주문 정보", properties: {} })

    expect(model?.description).toBe("주문 정보")
    expect(toSchema(model as SchemaModel)).toEqual({
      type: "object",
      description: "주문 정보",
      properties: {},
    })
  })

  it("reads fields, required flags and descriptions back", () => {
    const model = toModel({
      type: "object",
      properties: { a: { type: "string", description: "이름" }, b: { type: "number" } },
      required: ["a"],
    })

    expect(model?.fields).toEqual([
      { name: "a", required: true, description: "이름", value: { kind: "string" } },
      { name: "b", required: false, description: "", value: { kind: "number" } },
    ])
  })

  it("round-trips every schema it accepts", () => {
    // This is the property that matters: what the form gives back has to be what it was handed, or the
    // editor is losing part of the tenant's schema on open.
    const schemas = [
      { type: "object", properties: {} },
      { type: "object", properties: { a: { type: "integer" } } },
      { type: "object", properties: { a: { type: "boolean" } }, required: ["a"] },
      { type: "object", properties: { a: { type: "array", items: { type: "string" } } } },
      {
        type: "object",
        properties: {
          nested: { type: "object", properties: { x: { type: "number", description: "값" } }, required: ["x"] },
        },
      },
      { type: "object", description: "설명", properties: { a: { type: "string" } } },
    ]

    for (const schema of schemas) {
      const model = toModel(schema)
      expect(model, JSON.stringify(schema)).not.toBeNull()
      expect(toSchema(model as SchemaModel), JSON.stringify(schema)).toEqual(schema)
    }
  })

  it("refuses a schema whose root is not an object", () => {
    expect(toModel({ type: "string" })).toBeNull()
    expect(toModel({ type: ["object", "null"] })).toBeNull()
  })

  it("refuses the keywords the form cannot show, rather than dropping them", () => {
    // Every one of these is legal to the engine. Rewriting the schema without them would silently
    // change what the tenant's workflow accepts, and they would not find out until a run.
    const unrepresentable = [
      { type: "object", properties: { a: { anyOf: [{ type: "string" }, { type: "number" }] } } },
      { type: "object", properties: { a: { type: "string", enum: ["x", "y"] } } },
      { type: "object", properties: { a: { const: 1 } } },
      { type: "object", properties: { a: { type: "string", minLength: 1 } } },
      { type: "object", properties: { a: { type: "number", minimum: 0 } } },
      { type: "object", properties: { a: { type: "object", additionalProperties: true } } },
      { type: "object", properties: { a: { type: "array", items: true } } },
      // An array with a keyword the form has no column for, and one with no declared element type
      // at all -- the type select has nothing to show for either.
      { type: "object", properties: { a: { type: "array", items: { type: "string" }, minItems: 1 } } },
      { type: "object", properties: { a: { type: "array" } } },
      { type: "object", additionalProperties: false, properties: {} },
      { type: "object", properties: { a: { type: "string", title: "A" } } },
    ]

    for (const schema of unrepresentable) {
      expect(toModel(schema), JSON.stringify(schema)).toBeNull()
    }
  })

  it("refuses a required entry with no property to put it on", () => {
    // Legal JSON Schema, and there is no row to draw the checkbox on.
    expect(toModel({ type: "object", properties: { a: { type: "string" } }, required: ["ghost"] })).toBeNull()
  })

  it("refuses a malformed required or properties", () => {
    expect(toModel({ type: "object", properties: [] })).toBeNull()
    expect(toModel({ type: "object", properties: {}, required: "a" })).toBeNull()
    expect(toModel({ type: "object", properties: {}, required: [1] })).toBeNull()
  })

  it("refuses a property that is not a schema object", () => {
    expect(toModel({ type: "object", properties: { a: true } })).toBeNull()
  })

  it("normalizes an empty `required` away, which changes nothing", () => {
    // The one rewrite this module performs. `required: []` and no `required` accept exactly the same
    // values, so nothing the tenant declared is lost -- unlike every case above.
    const model = toModel({ type: "object", properties: { a: { type: "string" } }, required: [] })

    expect(toSchema(model as SchemaModel)).toEqual({ type: "object", properties: { a: { type: "string" } } })
  })
})
