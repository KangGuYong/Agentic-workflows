/** Reading the output schemas `/validate` reports, the way the engine reads them.
 *
 * This mirrors `engine/dsl/types.py` (`kinds_of`, `resolve_path`). It is a deliberate duplication and
 * the only one in this editor: autocomplete has to answer "what can follow this dot" between
 * keystrokes, and a round trip to the engine per character is not that. The engine stays the authority
 * -- everything here is a suggestion, and `/validate` is what decides whether a reference is legal.
 *
 * The rules are small and they are pinned by tests that name the engine function they follow, so a
 * change there shows up as a failure here rather than as an editor that quietly suggests fields the
 * engine will reject.
 */

export type JsonSchema = Record<string, unknown>

/** Korean names for the kinds, matching `engine/validator/refs.py::KIND_NAMES`. */
export const kindNames: Record<string, string> = {
  string: "문자열",
  number: "숫자",
  boolean: "참/거짓",
  object: "객체",
  array: "목록",
  null: "빈 값(null)",
  unknown: "알 수 없는 형식",
}

/** `engine/dsl/types.py::_JSON_TO_KIND`. `integer` and `number` are one runtime kind. */
const JSON_TO_KIND: Record<string, string> = {
  string: "string",
  number: "number",
  integer: "number",
  boolean: "boolean",
  object: "object",
  array: "array",
  null: "null",
}

function kindOfValue(value: unknown): string {
  if (value === null) return "null"
  if (typeof value === "boolean") return "boolean"
  if (typeof value === "number") return "number"
  if (typeof value === "string") return "string"
  return Array.isArray(value) ? "array" : "object"
}

function asSchema(value: unknown): JsonSchema | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as JsonSchema)
    : undefined
}

function isUnknown(schema: JsonSchema): boolean {
  return Object.keys(schema).length === 0
}

/** The value kinds a schema admits. `{"unknown"}` when it says nothing. */
export function kindsOf(schema: JsonSchema | undefined): Set<string> {
  if (schema === undefined || isUnknown(schema)) return new Set(["unknown"])

  const branches = [
    ...(Array.isArray(schema["anyOf"]) ? schema["anyOf"] : []),
    ...(Array.isArray(schema["oneOf"]) ? schema["oneOf"] : []),
  ]
  if (branches.length > 0) {
    const kinds = new Set<string>()
    for (const branch of branches) for (const kind of kindsOf(asSchema(branch))) kinds.add(kind)
    return kinds
  }

  const declared = schema["type"]
  if (declared === undefined) {
    if (Array.isArray(schema["enum"])) return new Set(schema["enum"].map(kindOfValue))
    if (schema["properties"] !== undefined) return new Set(["object"])
    if (schema["items"] !== undefined) return new Set(["array"])
    return new Set(["unknown"])
  }
  const names = Array.isArray(declared) ? declared : [declared]
  return new Set(names.map((name) => JSON_TO_KIND[String(name)] ?? "unknown"))
}

/** The sub-schema at `path`. `{}` means unknown (not checkable); `undefined` means the field is not
 * there at all, which is `engine/dsl/types.py::resolve_path` returning `None`. */
export function resolvePath(schema: JsonSchema | undefined, path: readonly string[]): JsonSchema | undefined {
  let current: JsonSchema | undefined = schema ?? {}
  for (const key of path) {
    if (current === undefined) return undefined
    if (isUnknown(current)) return {}

    if (/^\d+$/.test(key) && kindsOf(current).has("array")) {
      current = asSchema(current["items"]) ?? {}
      continue
    }
    const properties = asSchema(current["properties"])
    if (properties !== undefined) {
      if (key in properties) {
        current = asSchema(properties[key]) ?? {}
        continue
      }
      const extra = current["additionalProperties"]
      const extraSchema = asSchema(extra)
      if (extraSchema !== undefined) {
        current = extraSchema
        continue
      }
      if (extra === true) return {}
      return undefined
    }
    if (kindsOf(current).has("object")) return {}
    return undefined
  }
  return current
}

export interface SchemaProperty {
  name: string
  schema: JsonSchema
  required: boolean
}

/** The properties a person can be offered below this schema.
 *
 * Empty for a scalar -- which is what ends the completion chain -- and empty for a free-form map,
 * whose keys are whatever the run produces and so cannot be listed ahead of time.
 */
export function propertiesOf(schema: JsonSchema | undefined): SchemaProperty[] {
  const properties = schema === undefined ? undefined : asSchema(schema["properties"])
  if (properties === undefined) return []
  const required = new Set(
    Array.isArray(schema?.["required"]) ? (schema["required"] as unknown[]).map(String) : [],
  )
  return Object.entries(properties).map(([name, value]) => ({
    name,
    schema: asSchema(value) ?? {},
    required: required.has(name),
  }))
}
