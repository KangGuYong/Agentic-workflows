"use client"

import type { RegistryWidgetsType, WidgetProps } from "@rjsf/utils"

import { CONTROL as FIELD, TEMPLATE_WIDGETS } from "./templates"

/** Custom RJSF widgets for the two field kinds its defaults cannot render.
 *
 * Both are deliberately plain here. The template field gets `{{` autocomplete and reference chips in
 * Task 10, and the schema field gets a form editor in Task 11; until then they are honest textareas
 * that write the same values, so the panel is usable rather than half-present.
 */

export function TemplateWidget({ id, value, required, disabled, readonly, onChange, onBlur }: WidgetProps) {
  return (
    <textarea
      id={id}
      rows={4}
      className={`${FIELD} font-mono`}
      style={{ borderRadius: "var(--radius)" }}
      value={typeof value === "string" ? value : ""}
      required={required}
      disabled={disabled === true || readonly === true}
      onChange={(event) => onChange(event.target.value === "" ? undefined : event.target.value)}
      onBlur={(event) => onBlur(id, event.target.value)}
      placeholder="{{ 노드.필드 }} 로 이전 노드의 값을 씁니다"
    />
  )
}

export function JsonSchemaWidget({ id, value, disabled, readonly, onChange, onBlur }: WidgetProps) {
  const text = value === undefined ? "" : JSON.stringify(value, null, 2)
  return (
    <div>
      <textarea
        id={id}
        rows={8}
        className={`${FIELD} font-mono text-xs`}
        style={{ borderRadius: "var(--radius)" }}
        defaultValue={text}
        disabled={disabled === true || readonly === true}
        onBlur={(event) => {
          // Parsed on blur, not on every keystroke: half-typed JSON is invalid JSON, and reporting that
          // on each character would make the field unusable.
          try {
            onChange(event.target.value.trim() === "" ? undefined : JSON.parse(event.target.value))
          } catch {
            // Left as typed. The engine's `/validate` is the authority on whether a schema is usable,
            // and Task 11 replaces this textarea with a form that cannot produce invalid JSON at all.
          }
          onBlur(id, event.target.value)
        }}
      />
      <p className="mt-1 text-xs text-fg-faint">JSON 형식으로 입력합니다. 저장할 때 형식을 확인합니다.</p>
    </div>
  )
}

export const WIDGETS: RegistryWidgetsType = {
  ...TEMPLATE_WIDGETS,
  template: TemplateWidget,
  jsonSchema: JsonSchemaWidget,
}
