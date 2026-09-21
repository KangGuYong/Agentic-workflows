import type { NodeType } from "@/lib/palette"

/** The uiSchema the settings panel hands RJSF (3 설계 §5.3).
 *
 * The engine has no display strings, so Korean titles live here. Widget choice does *not*: it is
 * derived from the schema itself, because a hand-kept list of which fields are templates is exactly the
 * thing that goes stale the moment a node type gains a field.
 */

export interface FieldUi {
  "ui:widget"?: string
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
}

function isTemplate(property: unknown): boolean {
  return typeof property === "object" && property !== null && (property as Record<string, unknown>)["x-template"] === true
}

export function buildUiSchema(nodeType: NodeType): UiSchema {
  const properties = (nodeType.configSchema.properties ?? {}) as Record<string, unknown>
  const schemaFields = new Set(SCHEMA_FIELDS[nodeType.type] ?? [])
  const ui: UiSchema = { "ui:order": [...(ORDER[nodeType.type] ?? []), "*"] }

  for (const [name, property] of Object.entries(properties)) {
    const field: FieldUi = {}
    if (schemaFields.has(name)) field["ui:widget"] = "jsonSchema"
    else if (isTemplate(property)) field["ui:widget"] = "template"
    const title = TITLES[name]
    if (title !== undefined) field["ui:title"] = title
    if (Object.keys(field).length > 0) ui[name] = field
  }
  return ui
}
