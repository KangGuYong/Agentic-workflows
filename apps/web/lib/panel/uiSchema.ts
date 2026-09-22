import type { NodeType } from "@/lib/palette"

/** The uiSchema the settings panel hands RJSF (3 설계 §5.3).
 *
 * The engine has no display strings, so Korean titles live here. Widget choice does *not*: it is
 * derived from the schema itself, because a hand-kept list of which fields are templates is exactly the
 * thing that goes stale the moment a node type gains a field.
 */

export interface FieldUi {
  "ui:widget"?: string
  /** For a property RJSF would otherwise render itself, such as an `anyOf`. See `JsonSchemaField`. */
  "ui:field"?: string
  "ui:title"?: string
  "ui:help"?: string
}

/** An intersection would make `ui:order` collide with the index signature, so the order key is part of
 * the value union instead. RJSF reads both shapes from the same object. */
export interface UiSchema {
  "ui:order"?: string[]
  [field: string]: FieldUi | string[] | undefined
}

/** Properties that hold a JSON Schema rather than a value, keyed by the node type that owns them.
 *
 * Keyed by type deliberately: a future node with a property called `inputs` that is not a schema must
 * not get the schema editor. */
export const SCHEMA_FIELDS: Record<string, readonly string[]> = {
  start: ["inputs"],
  llm: ["outputSchema"],
}

/** Korean titles. A property the editor has not seen gets none, and RJSF falls back to the schema's own
 * title or the property name -- better than a made-up label. */
const TITLES: Record<string, string> = {
  model: "모델",
  system: "시스템 지시",
  prompt: "프롬프트",
  temperature: "창의성",
  outputSchema: "출력 형식",
  inputs: "입력 형식",
  outputs: "출력",
  template: "템플릿",
  format: "형식",
  input: "분류할 값",
  categories: "분류 항목",
  instructions: "분류 기준",
  conditions: "조건",
  combinator: "조건 결합",
  mode: "합치는 방식",
  message: "안내 메시지",
  review: "검토할 값",
  allowEdit: "값 수정 허용",
  method: "메서드",
  url: "주소",
  headers: "헤더",
  body: "본문",
  bodyFormat: "본문 형식",
  sendIdempotencyKey: "멱등 키 전송",
  knowledgeBase: "지식베이스",
  query: "질문",
  topK: "검색 개수",
  minScore: "최소 점수",
  hits: "검색 결과",
  topN: "남길 개수",
}

/** Field order per node type: what a person fills first, first. `*` catches anything not listed --
 * without it RJSF throws on a property the order does not mention, so an engine adding a config field
 * would blank the panel. */
const ORDER: Record<string, string[]> = {
  start: ["inputs"],
  end: ["outputs"],
  template: ["template", "format"],
  llm: ["model", "system", "prompt", "temperature", "outputSchema"],
  classifier: ["model", "input", "categories", "instructions", "temperature"],
  condition: ["conditions", "combinator"],
  merge: ["mode"],
  human_approval: ["message", "review", "allowEdit"],
  http_request: ["method", "url", "headers", "body", "bodyFormat", "sendIdempotencyKey"],
  kb_search: ["knowledgeBase", "query", "topK", "minScore"],
  rerank: ["query", "hits", "topN", "minScore"],
}

function isTemplate(property: unknown): boolean {
  return typeof property === "object" && property !== null && (property as Record<string, unknown>)["x-template"] === true
}

function isKnowledgeBase(property: unknown): boolean {
  return typeof property === "object" && property !== null && (property as Record<string, unknown>)["x-knowledge-base"] === true
}

export function buildUiSchema(nodeType: NodeType): UiSchema {
  const properties = (nodeType.configSchema.properties ?? {}) as Record<string, unknown>
  const schemaFields = new Set(SCHEMA_FIELDS[nodeType.type] ?? [])
  const ui: UiSchema = { "ui:order": [...(ORDER[nodeType.type] ?? []), "*"] }

  for (const [name, property] of Object.entries(properties)) {
    const field: FieldUi = {}
    // A field, not a widget: the engine declares these as `anyOf: [object, null]`, and RJSF renders an
    // `anyOf` with its own option selector without ever consulting `ui:widget`.
    if (schemaFields.has(name)) field["ui:field"] = "jsonSchema"
    else if (isKnowledgeBase(property)) field["ui:widget"] = "knowledgeBase"
    else if (isTemplate(property)) field["ui:widget"] = "template"
    const title = TITLES[name]
    if (title !== undefined) field["ui:title"] = title
    if (Object.keys(field).length > 0) ui[name] = field
  }
  return ui
}

/** Whether this node type has any template field at all, which decides if the rules are worth showing. */
export function hasTemplateField(nodeType: NodeType): boolean {
  const properties = (nodeType.configSchema.properties ?? {}) as Record<string, unknown>
  return Object.values(properties).some(isTemplate)
}

/** The config schema with the optional-object `anyOf` on schema fields collapsed away.
 *
 * `ui:field` replaces how a field's *value* is edited, but RJSF still renders its own branch selector
 * for an `anyOf` alongside it -- so the panel showed the schema editor with a stray
 * "출력 형식 option 2" dropdown under it. There is nothing for a person to choose there: the branches
 * are "an object" and "nothing", and an empty editor already means nothing.
 *
 * Only `anyOf: [<object>, {type: "null"}]` is collapsed, and only on a declared schema field. Any other
 * `anyOf` is left for RJSF to render, because dropping a branch the editor cannot represent would
 * silently narrow what the tenant is allowed to save.
 */
export function collapseOptionalSchemas(nodeType: NodeType): Record<string, unknown> {
  const schemaFields = SCHEMA_FIELDS[nodeType.type]
  const properties = nodeType.configSchema.properties as Record<string, unknown> | undefined
  if (schemaFields === undefined || properties === undefined) return nodeType.configSchema

  const collapsed: Record<string, unknown> = { ...properties }
  for (const name of schemaFields) {
    const objectBranch = optionalObjectBranch(properties[name])
    if (objectBranch !== undefined) collapsed[name] = objectBranch
  }
  return { ...nodeType.configSchema, properties: collapsed }
}

/** The object branch of `anyOf: [<object>, {type: "null"}]`, or undefined if that is not the shape. */
function optionalObjectBranch(property: unknown): Record<string, unknown> | undefined {
  if (typeof property !== "object" || property === null) return undefined
  const branches = (property as Record<string, unknown>)["anyOf"]
  if (!Array.isArray(branches) || branches.length !== 2) return undefined

  const objects = branches.filter(
    (branch): branch is Record<string, unknown> =>
      typeof branch === "object" && branch !== null && (branch as Record<string, unknown>)["type"] === "object",
  )
  const nulls = branches.filter(
    (branch) =>
      typeof branch === "object" && branch !== null && (branch as Record<string, unknown>)["type"] === "null",
  )
  if (objects.length !== 1 || nulls.length !== 1) return undefined
  // Keep the property's own title: it is what the label reads.
  const { anyOf: _dropped, ...rest } = property as Record<string, unknown>
  return { ...rest, ...objects[0] }
}
