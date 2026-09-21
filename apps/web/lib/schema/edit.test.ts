import { describe, expect, it } from "vitest"

import {
  addField,
  canOpen,
  duplicateNames,
  fieldsAt,
  freeName,
  itemTypeOf,
  losesFields,
  removeField,
  setField,
  setFieldType,
  setItemType,
  trail,
} from "./edit"
import { emptyModel, toSchema, type FieldModel, type SchemaModel } from "./model"

function scalar(name: string): FieldModel {
  return { name, required: false, description: "", value: { kind: "string" } }
}

/** A root with `user` (an object holding `id`) and `tags` (a list of objects holding `label`). */
const NESTED: SchemaModel = {
  description: "",
  fields: [
    { name: "user", required: false, description: "", value: { kind: "object", fields: [scalar("id")] } },
    {
      name: "tags",
      required: false,
      description: "",
      value: { kind: "array", item: { kind: "object", fields: [scalar("label")] } },
    },
  ],
}

describe("navigating", () => {
  it("finds the fields at the root", () => {
    expect(fieldsAt(NESTED, [])?.map((field) => field.name)).toEqual(["user", "tags"])
  })

  it("descends into an object", () => {
    expect(fieldsAt(NESTED, [0])?.map((field) => field.name)).toEqual(["id"])
  })

  it("sees through a list to its element, which is what has the fields", () => {
    expect(fieldsAt(NESTED, [1])?.map((field) => field.name)).toEqual(["label"])
  })

  it("is null for a path into something with no fields", () => {
    expect(fieldsAt({ description: "", fields: [scalar("a")] }, [0])).toBeNull()
    expect(fieldsAt(NESTED, [9])).toBeNull()
  })

  it("names the trail for the breadcrumb", () => {
    expect(trail(NESTED, [1])).toEqual(["tags"])
    expect(trail(NESTED, [])).toEqual([])
  })

  it("gives what it can for a trail that no longer resolves", () => {
    // A path survives in component state across an edit that removed what it pointed at.
    expect(trail(NESTED, [0, 5, 7])).toEqual(["user"])
  })

  it("stops at a field that has nothing under it", () => {
    // The other way a path goes stale: the row it passed through was retyped to a scalar. Walking on
    // would re-read the same level and name it twice.
    expect(trail({ description: "", fields: [scalar("a")] }, [0, 0])).toEqual(["a"])
  })

  it("says which rows can be opened", () => {
    expect(canOpen(NESTED.fields[0] as FieldModel)).toBe(true)
    expect(canOpen(NESTED.fields[1] as FieldModel)).toBe(true)
    expect(canOpen(scalar("a"))).toBe(false)
  })
})

describe("adding and removing", () => {
  it("adds a string field with a free name", () => {
    const model = addField(emptyModel(), [])

    expect(model.fields).toEqual([{ name: "field1", required: false, description: "", value: { kind: "string" } }])
  })

  it("does not reuse a name already at that level", () => {
    expect(freeName([scalar("field1"), scalar("field2")])).toBe("field3")
  })

  it("adds inside a nested object without touching the root", () => {
    const model = addField(NESTED, [0])

    expect(fieldsAt(model, [0])?.map((field) => field.name)).toEqual(["id", "field1"])
    expect(model.fields.map((field) => field.name)).toEqual(["user", "tags"])
  })

  it("adds inside a list's element", () => {
    expect(fieldsAt(addField(NESTED, [1]), [1])?.map((f) => f.name)).toEqual(["label", "field1"])
  })

  it("removes by index", () => {
    expect(removeField(NESTED, [], 0).fields.map((field) => field.name)).toEqual(["tags"])
  })

  it("leaves the model alone for a path that does not resolve", () => {
    // A stale path must not corrupt the document; it just does nothing.
    expect(addField(NESTED, [9]).fields).toEqual(NESTED.fields)
  })
})

describe("editing a row", () => {
  it("renames without disturbing the others", () => {
    const model = setField(NESTED, [], 0, { name: "account" })

    expect(model.fields.map((field) => field.name)).toEqual(["account", "tags"])
  })

  it("sets required and description", () => {
    const model = setField(NESTED, [0], 0, { required: true, description: "식별자" })

    expect(fieldsAt(model, [0])?.[0]).toMatchObject({ required: true, description: "식별자" })
  })
})

describe("changing a type", () => {
  it("keeps the nested fields when going 객체 -> 목록 and back", () => {
    // The contents did not change, only how many of them there are. Making someone retype the fields
    // for that is throwing their work away.
    const toList = setFieldType(NESTED, [], 0, "array")
    expect(fieldsAt(toList, [0])?.map((field) => field.name)).toEqual(["id"])
    expect(itemTypeOf((toList.fields[0] as FieldModel).value)).toBe("object")

    const back = setFieldType(toList, [], 0, "object")
    expect(fieldsAt(back, [0])?.map((field) => field.name)).toEqual(["id"])
  })

  it("drops them when the new type has nowhere to put them", () => {
    const model = setFieldType(NESTED, [], 0, "string")

    expect(fieldsAt(model, [0])).toBeNull()
    expect(toSchema(model).properties).toMatchObject({ user: { type: "string" } })
  })

  it("says in advance which change loses fields, so the form can ask first", () => {
    const user = NESTED.fields[0] as FieldModel

    expect(losesFields(user, "string")).toBe(true)
    expect(losesFields(user, "array")).toBe(false)
    expect(losesFields(user, "object")).toBe(false)
    // Nothing to lose: an empty object has no fields to warn about.
    expect(losesFields({ ...user, value: { kind: "object", fields: [] } }, "string")).toBe(false)
    expect(losesFields(scalar("a"), "number")).toBe(false)
  })

  it("changes a list's element type", () => {
    const model = setItemType(NESTED, [], 1, "string")

    expect(itemTypeOf((model.fields[1] as FieldModel).value)).toBe("string")
    expect(toSchema(model).properties).toMatchObject({ tags: { type: "array", items: { type: "string" } } })
  })

  it("leaves a non-list field alone when asked for an element type", () => {
    expect(setItemType(NESTED, [], 0, "number")).toEqual(NESTED)
  })
})

describe("duplicate names", () => {
  it("finds a name used twice at one level", () => {
    // JSON objects keep the last one, so the engine never sees the duplicate and never complains --
    // the field would just vanish on save. The form has to say so itself.
    expect(duplicateNames([scalar("a"), scalar("b"), scalar("a")])).toEqual(new Set(["a"]))
  })

  it("is empty when every name is distinct", () => {
    expect(duplicateNames([scalar("a"), scalar("b")]).size).toBe(0)
  })

  it("does not care about the same name at a different level", () => {
    const model = addField(setField(NESTED, [0], 0, { name: "user" }), [])

    expect(duplicateNames(fieldsAt(model, []) ?? []).size).toBe(0)
  })
})

describe("naming a new field", () => {
  it("finds a free name however many are taken", () => {
    // The bound is `fields.length + 1` names for `fields.length` fields, so one of them is always free.
    const fields = Array.from({ length: 12 }, (_, index) => scalar(`field${index + 1}`))

    expect(freeName(fields)).toBe("field13")
  })

  it("does not skip a hole in the middle", () => {
    expect(freeName([scalar("field1"), scalar("field3")])).toBe("field2")
  })
})
