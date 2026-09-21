import { describe, expect, it } from "vitest"

import { kindsOf, kindNames, propertiesOf, resolvePath } from "./schema"

describe("kindsOf", () => {
  it("reads a declared type", () => {
    expect(kindsOf({ type: "string" })).toEqual(new Set(["string"]))
  })

  it("maps integer onto number, as the engine does", () => {
    // `engine/dsl/types.py::_JSON_TO_KIND`. Two names for one runtime kind; the editor must agree with
    // the engine or it will label a field the engine accepts as a mismatch.
    expect(kindsOf({ type: "integer" })).toEqual(new Set(["number"]))
  })

  it("unions a type list and the anyOf/oneOf branches", () => {
    expect(kindsOf({ type: ["string", "null"] })).toEqual(new Set(["string", "null"]))
    expect(kindsOf({ anyOf: [{ type: "string" }, { type: "number" }] })).toEqual(
      new Set(["string", "number"]),
    )
  })

  it("infers object or array from the shape when no type is declared", () => {
    expect(kindsOf({ properties: {} })).toEqual(new Set(["object"]))
    expect(kindsOf({ items: { type: "string" } })).toEqual(new Set(["array"]))
  })

  it("reads the kinds of an enum's values", () => {
    expect(kindsOf({ enum: ["a", 1] })).toEqual(new Set(["string", "number"]))
  })

  it("calls an empty or absent schema unknown rather than guessing", () => {
    expect(kindsOf({})).toEqual(new Set(["unknown"]))
    expect(kindsOf(undefined)).toEqual(new Set(["unknown"]))
  })

  it("has a Korean name for every kind it can return", () => {
    // A missing entry would show the English kind next to Korean ones in the completion list.
    for (const kind of ["string", "number", "boolean", "object", "array", "null", "unknown"]) {
      expect(kindNames[kind]).toBeTruthy()
    }
  })
})

describe("resolvePath", () => {
  const schema = {
    type: "object",
    properties: {
      text: { type: "string" },
      meta: { type: "object", properties: { tokens: { type: "integer" } } },
      items: { type: "array", items: { type: "string" } },
    },
  }

  it("walks properties", () => {
    expect(resolvePath(schema, ["meta", "tokens"])).toEqual({ type: "integer" })
  })

  it("is undefined for a field the schema does not have", () => {
    // Distinct from `{}`: "not there" is an error the engine reports, "unknown" is fine.
    expect(resolvePath(schema, ["nope"])).toBeUndefined()
  })

  it("walks into an array by index", () => {
    expect(resolvePath(schema, ["items", "0"])).toEqual({ type: "string" })
  })

  it("follows additionalProperties only when `properties` is present, as the engine does", () => {
    // Verified against `engine/dsl/types.py::resolve_path`, which reaches `additionalProperties` only
    // inside the `props is not None` branch. Without a `properties` key it falls through to
    // "an object, contents unknown" -- `{}`. The editor has to agree: suggesting `number` where the
    // engine says "unknown" would flag a mismatch the engine never reports.
    expect(resolvePath({ type: "object", additionalProperties: { type: "number" } }, ["k"])).toEqual({})
    expect(
      resolvePath({ type: "object", properties: {}, additionalProperties: { type: "number" } }, ["k"]),
    ).toEqual({ type: "number" })
  })

  it("is unknown, not missing, below an unknown schema", () => {
    expect(resolvePath({}, ["a", "b"])).toEqual({})
  })

  it("is unknown, not missing, for a schema that allows any extra property", () => {
    // `additionalProperties: true` says the field may be there and says nothing about its type. That is
    // "unknown", not "does not exist" -- reporting the latter would flag a reference the engine accepts.
    const open = { type: "object", properties: { a: { type: "string" } }, additionalProperties: true }

    expect(resolvePath(open, ["whatever"])).toEqual({})
    expect(resolvePath(open, ["a"])).toEqual({ type: "string" })
  })

  it("is missing when the schema closes the door on extra properties", () => {
    // The control: `additionalProperties` absent means the listed properties are all there are.
    expect(resolvePath({ type: "object", properties: { a: {} } }, ["whatever"])).toBeUndefined()
  })

  it("is the schema itself for an empty path", () => {
    expect(resolvePath(schema, [])).toBe(schema)
  })
})

describe("propertiesOf", () => {
  it("lists declared properties with their sub-schemas", () => {
    const schema = { type: "object", properties: { a: { type: "string" }, b: { type: "number" } } }

    expect(propertiesOf(schema).map((property) => property.name)).toEqual(["a", "b"])
  })

  it("is empty for a scalar, which is what ends the completion chain", () => {
    expect(propertiesOf({ type: "string" })).toEqual([])
  })

  it("is empty for a free-form map, whose keys the editor cannot know", () => {
    expect(propertiesOf({ type: "object", additionalProperties: { type: "string" } })).toEqual([])
  })

  it("marks the required ones, so the list can say which are always present", () => {
    const schema = { type: "object", properties: { a: {}, b: {} }, required: ["a"] }

    expect(propertiesOf(schema).find((property) => property.name === "a")?.required).toBe(true)
    expect(propertiesOf(schema).find((property) => property.name === "b")?.required).toBe(false)
  })
})
