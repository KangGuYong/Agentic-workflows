import type { JsonSchema } from "@/lib/template/schema"

/** The form editor's model of a tenant JSON Schema (3 설계 §7).
 *
 * The engine accepts a subset of JSON Schema (`engine/jsondata.py::schema_problems`). The **form**
 * represents a smaller subset still: named fields with a type, a required flag and a description,
 * nested through objects and arrays. Everything else the engine allows -- `anyOf`, `enum`, `const`,
 * bounds, `additionalProperties`, a `type` list -- is legal and unrepresentable here.
 *
 * The rule that shapes this module: **a schema the form cannot represent is never silently rewritten.**
 * `toModel` answers `null` for it, the toggle goes to JSON and stays there, and the tenant's schema
 * survives untouched. A form editor that quietly drops the keyword it did not understand is worse than
 * no form editor, because the loss is invisible until a run produces the wrong shape.
 */

export const FIELD_TYPES = ["string", "number", "integer", "boolean", "array", "object"] as const

export type FieldType = (typeof FIELD_TYPES)[number]

/** Korean names for the type column. */
export const TYPE_NAMES: Record<FieldType, string> = {
  string: "문자열",
  number: "숫자",
  integer: "정수",
  boolean: "참/거짓",
  array: "목록",
  object: "객체",
}

export type ValueModel =
  | { kind: "string" | "number" | "integer" | "boolean" }
  | { kind: "object"; fields: FieldModel[] }
  | { kind: "array"; item: ValueModel }

export interface FieldModel {
  name: string
  required: boolean
  description: string
  value: ValueModel
}

/** The root is always an object: both `start.inputs` and `llm.outputSchema` describe a set of named
 * values, and the engine's nodes read them that way.
 *
 * The root carries its own description because nothing else can. A field's description belongs to its
 * row; the root's has no row, and reading it without writing it back was a silent drop -- the exact
 * thing this module exists to prevent. The form shows it as a 설명 box above the table.
 */
export interface SchemaModel {
  description: string
  fields: FieldModel[]
}

export function emptyModel(): SchemaModel {
  return { description: "", fields: [] }
}

/** Keys the form knows how to show. Anything else makes a schema unrepresentable. */
const OBJECT_KEYS = new Set(["type", "properties", "required", "description"])
const SCALAR_KEYS = new Set(["type", "description"])
const ARRAY_KEYS = new Set(["type", "items", "description"])

function isFieldType(value: unknown): value is FieldType {
  return typeof value === "string" && (FIELD_TYPES as readonly string[]).includes(value)
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function valueOf(schema: Record<string, unknown>): ValueModel | null {
  const type = schema["type"]
  if (!isFieldType(type)) return null

  if (type === "object") {
    if (!hasOnly(schema, OBJECT_KEYS)) return null
    const fields = fieldsOf(schema)
    return fields === null ? null : { kind: "object", fields }
  }
  if (type === "array") {
    if (!hasOnly(schema, ARRAY_KEYS)) return null
    const items = schema["items"]
    // `items: true` and `items: false` are legal to the engine and mean "anything" and "nothing".
    // Neither is a type this form can put in its type column.
    if (!isPlainObject(items)) return null
    const item = valueOf(items)
    return item === null ? null : { kind: "array", item }
  }
  if (!hasOnly(schema, SCALAR_KEYS)) return null
  return { kind: type }
}

function hasOnly(schema: Record<string, unknown>, allowed: ReadonlySet<string>): boolean {
  return Object.keys(schema).every((key) => allowed.has(key))
}

function fieldsOf(schema: Record<string, unknown>): FieldModel[] | null {
  const properties = schema["properties"]
  if (properties !== undefined && !isPlainObject(properties)) return null
  const entries = properties === undefined ? [] : Object.entries(properties)

  const required = schema["required"]
  if (required !== undefined && (!Array.isArray(required) || required.some((n) => typeof n !== "string"))) {
    return null
  }
  const names = new Set(entries.map(([name]) => name))
  // `required: ["ghost"]` naming a property that is not there is legal JSON Schema and has no row to
  // put a checkbox on. Refusing is the honest answer; inventing the row is not.
  if (Array.isArray(required) && required.some((name) => !names.has(String(name)))) return null
  const requiredNames = new Set((required ?? []).map(String))

  const fields: FieldModel[] = []
  for (const [name, raw] of entries) {
    if (!isPlainObject(raw)) return null
    const value = valueOf(raw)
    if (value === null) return null
    const description = raw["description"]
    if (description !== undefined && typeof description !== "string") return null
    fields.push({
      name,
      required: requiredNames.has(name),
      description: description ?? "",
      value,
    })
  }
  return fields
}

/** The form's model of `schema`, or `null` when the form cannot represent it without losing something.
 *
 * An absent schema and `{}` both mean "nothing declared yet", which is the state a new node is in, and
 * both open as an empty form.
 */
export function toModel(schema: JsonSchema | undefined | null): SchemaModel | null {
  if (schema === undefined || schema === null) return emptyModel()
  if (!isPlainObject(schema)) return null
  if (Object.keys(schema).length === 0) return emptyModel()

  if (schema["type"] !== "object") return null
  if (!hasOnly(schema, OBJECT_KEYS)) return null
  const description = schema["description"]
  if (description !== undefined && typeof description !== "string") return null
  const fields = fieldsOf(schema)
  return fields === null ? null : { description: description ?? "", fields }
}

function schemaOfValue(value: ValueModel, description: string): JsonSchema {
  const described = description === "" ? {} : { description }
  if (value.kind === "object") return { type: "object", ...described, ...objectBody(value.fields) }
  if (value.kind === "array") return { type: "array", ...described, items: schemaOfValue(value.item, "") }
  return { type: value.kind, ...described }
}

function objectBody(fields: readonly FieldModel[]): JsonSchema {
  const properties: Record<string, JsonSchema> = {}
  for (const field of fields) properties[field.name] = schemaOfValue(field.value, field.description)
  const required = fields.filter((field) => field.required).map((field) => field.name)
  // `properties` is always written, even empty: `{}` means "any object" to the engine, while
  // `{type: "object", properties: {}}` means "an object with no declared fields" -- which is what
  // removing the last row actually did.
  return required.length === 0 ? { properties } : { properties, required }
}

export function toSchema(model: SchemaModel): JsonSchema {
  const described = model.description === "" ? {} : { description: model.description }
  return { type: "object", ...described, ...objectBody(model.fields) }
}
