import type { FieldModel, FieldType, SchemaModel, ValueModel } from "./model"

/** Edits on the form model, as pure functions (3 설계 §7).
 *
 * The form shows one level at a time, so every edit is addressed by a **path**: the indices of the
 * field rows that were opened to get here. An array is transparent to a path -- opening a 목록 of
 * 객체 lands on the element's fields, because the element is what has fields.
 *
 * Indices rather than names: names are edited in place and are empty, duplicated or half-typed for as
 * long as someone is typing one.
 */

export type Path = readonly number[]

function fieldsOfValue(value: ValueModel): FieldModel[] | null {
  if (value.kind === "object") return value.fields
  if (value.kind === "array") return fieldsOfValue(value.item)
  return null
}

/** The field list at `path`, or `null` if the path does not lead to one. */
export function fieldsAt(model: SchemaModel, path: Path): FieldModel[] | null {
  let fields: FieldModel[] = model.fields
  for (const index of path) {
    const field = fields[index]
    if (field === undefined) return null
    const inner = fieldsOfValue(field.value)
    if (inner === null) return null
    fields = inner
  }
  return fields
}

/** The names along `path`, for the breadcrumb. A path that no longer resolves gives what it can. */
export function trail(model: SchemaModel, path: Path): string[] {
  const names: string[] = []
  let fields: FieldModel[] = model.fields
  for (const index of path) {
    const field = fields[index]
    if (field === undefined) break
    names.push(field.name)
    const inner = fieldsOfValue(field.value)
    if (inner === null) break
    fields = inner
  }
  return names
}

function mapValueFields(value: ValueModel, change: (fields: FieldModel[]) => FieldModel[]): ValueModel {
  if (value.kind === "object") return { kind: "object", fields: change(value.fields) }
  if (value.kind === "array") return { kind: "array", item: mapValueFields(value.item, change) }
  return value
}

/** `model` with the field list at `path` replaced by `change(...)`. Unknown paths are left alone. */
export function updateAt(
  model: SchemaModel,
  path: Path,
  change: (fields: FieldModel[]) => FieldModel[],
): SchemaModel {
  if (path.length === 0) return { ...model, fields: change(model.fields) }
  return { ...model, fields: updateFields(model.fields, path, change) }
}

function updateFields(
  fields: readonly FieldModel[],
  path: Path,
  change: (fields: FieldModel[]) => FieldModel[],
): FieldModel[] {
  const [index, ...rest] = path
  // An index that is not there simply matches no row, so a stale path is a no-op without a guard.
  return fields.map((field, at) => {
    if (at !== index) return field
    return {
      ...field,
      value: mapValueFields(field.value, (inner) =>
        rest.length === 0 ? change(inner) : updateFields(inner, rest, change),
      ),
    }
  })
}

/** A field name that is not already taken at this level.
 *
 * Bounded, not open-ended. Among `field1 .. field(n+1)` at least one is free when there are `n` fields,
 * so the loop is provably finished by then -- and an unbounded search here is a synchronous spin that
 * no test timeout can interrupt, because it never yields.
 */
export function freeName(fields: readonly FieldModel[]): string {
  const taken = new Set(fields.map((field) => field.name))
  for (let n = 1; n <= fields.length + 1; n += 1) {
    const name = `field${n}`
    if (!taken.has(name)) return name
  }
  /* c8 ignore next -- unreachable by the pigeonhole argument above; here so the function cannot spin. */
  return `field${fields.length + 1}`
}

export function addField(model: SchemaModel, path: Path): SchemaModel {
  return updateAt(model, path, (fields) => [
    ...fields,
    { name: freeName(fields), required: false, description: "", value: { kind: "string" } },
  ])
}

export function removeField(model: SchemaModel, path: Path, index: number): SchemaModel {
  return updateAt(model, path, (fields) => fields.filter((_, at) => at !== index))
}

export function setField(
  model: SchemaModel,
  path: Path,
  index: number,
  change: Partial<Pick<FieldModel, "name" | "required" | "description">>,
): SchemaModel {
  return updateAt(model, path, (fields) =>
    fields.map((field, at) => (at === index ? { ...field, ...change } : field)),
  )
}

function newValue(type: FieldType, previous: ValueModel): ValueModel {
  if (type === "object") {
    // Keep the fields when the shape still has a place for them: 목록 of 객체 <-> 객체 is a change of
    // arity, not of contents, and re-typing the fields would be the tenant's work thrown away.
    const kept = fieldsOfValue(previous)
    return { kind: "object", fields: kept ?? [] }
  }
  if (type === "array") return { kind: "array", item: previous.kind === "array" ? previous.item : previous }
  return { kind: type }
}

/** Change a field's type.
 *
 * Nested fields survive a change that still has somewhere to put them (객체 <-> 목록) and are lost by
 * one that does not (객체 -> 문자열). `losesFields` says which is which, so the form can ask first
 * rather than discard silently.
 */
export function setFieldType(model: SchemaModel, path: Path, index: number, type: FieldType): SchemaModel {
  return updateAt(model, path, (fields) =>
    fields.map((field, at) => (at === index ? { ...field, value: newValue(type, field.value) } : field)),
  )
}

/** Change the element type of a 목록 field. A non-array field is left alone. */
export function setItemType(model: SchemaModel, path: Path, index: number, type: FieldType): SchemaModel {
  return updateAt(model, path, (fields) =>
    fields.map((field, at) => {
      if (at !== index || field.value.kind !== "array") return field
      return { ...field, value: { kind: "array", item: newValue(type, field.value.item) } }
    }),
  )
}

/** Whether changing this field to `type` would throw away fields nested under it. */
export function losesFields(field: FieldModel, type: FieldType): boolean {
  if (type === "object" || type === "array") return false
  const inner = fieldsOfValue(field.value)
  return inner !== null && inner.length > 0
}

/** The type shown in a row's type column, and the element type for a 목록. */
export function typeOf(value: ValueModel): FieldType {
  return value.kind
}

export function itemTypeOf(value: ValueModel): FieldType | null {
  return value.kind === "array" ? value.item.kind : null
}

/** Whether a row can be opened: it has a field list under it. */
export function canOpen(field: FieldModel): boolean {
  return fieldsOfValue(field.value) !== null
}

/** Names used more than once at this level. The engine cannot see the duplicate -- JSON objects keep
 * the last one -- so the form has to say so itself before a field silently disappears on save. */
export function duplicateNames(fields: readonly FieldModel[]): Set<string> {
  const seen = new Set<string>()
  const twice = new Set<string>()
  for (const field of fields) {
    if (seen.has(field.name)) twice.add(field.name)
    seen.add(field.name)
  }
  return twice
}
