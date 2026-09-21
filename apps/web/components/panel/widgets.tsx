"use client"

import type { FieldProps, RegistryFieldsType, RegistryWidgetsType, WidgetProps } from "@rjsf/utils"

import type { TemplateContext } from "@/lib/template/context"

import { CONTROL as FIELD, TEMPLATE_WIDGETS } from "./templates"
import { TemplateEditor } from "./TemplateEditor"

/** Custom RJSF widgets for the two field kinds its defaults cannot render.
 *
 * The template field is the CodeMirror editor from Task 10. The schema field is still an honest
 * textarea until Task 11 replaces it with a form that cannot produce invalid JSON at all; it writes the
 * same value either way, so the panel is usable rather than half-present.
 */

export function TemplateWidget({ id, value, disabled, readonly, onChange, onBlur, registry }: WidgetProps) {
  // RJSF has no way to hand a widget something the schema does not describe, so the workflow context
  // rides on `formContext`. Absent means "no other nodes to complete against" -- an editor with an
  // empty completion list, not a crash.
  const context = (registry.formContext as { template?: TemplateContext }).template ?? EMPTY_CONTEXT

  return (
    <TemplateEditor
      id={id}
      value={typeof value === "string" ? value : ""}
      context={context}
      disabled={disabled === true || readonly === true}
      onChange={(next) => onChange(next === "" ? undefined : next)}
      onBlur={() => onBlur(id, value)}
    />
  )
}

const EMPTY_CONTEXT: TemplateContext = { nodes: [], guaranteed: null }

/** The schema field.
 *
 * A **field**, not a widget. The engine declares these as `anyOf: [{type: object}, {type: null}]` --
 * optional -- and RJSF renders any `anyOf` with its own option selector before it ever looks at
 * `ui:widget`. Registered as a widget, this never appeared: the panel showed a bare number input
 * holding the selected branch index instead.
 */
export function JsonSchemaField({ schema, fieldPathId, formData, disabled, readonly, onChange, uiSchema }: FieldProps) {
  const id = fieldPathId.$id
  const text = formData === undefined || formData === null ? "" : JSON.stringify(formData, null, 2)
  const title = uiSchema?.["ui:title"] ?? schema.title ?? ""

  return (
    <div className="mb-4">
      {title === "" ? null : (
        <label htmlFor={id} className="instrument-label mb-1 block">
          {title}
        </label>
      )}
      <textarea
        id={id}
        rows={6}
        aria-describedby={`${id}-help`}
        className={`${FIELD} font-mono text-xs`}
        style={{ borderRadius: "var(--radius)" }}
        defaultValue={text}
        disabled={disabled === true || readonly === true}
        onBlur={(event) => {
          // Parsed on blur, not on every keystroke: half-typed JSON is invalid JSON, and reporting that
          // on each character would make the field unusable.
          try {
            // A field's `onChange` carries the path, unlike a widget's: it writes into the form data
            // itself rather than into one already-located slot.
            const next = event.target.value.trim() === "" ? undefined : JSON.parse(event.target.value)
            onChange(next, fieldPathId.path)
          } catch {
            // Left as typed. The engine's `/validate` is the authority on whether a schema is usable,
            // and Task 11 replaces this textarea with a form that cannot produce invalid JSON at all.
          }
        }}
      />
      <p id={`${id}-help`} className="mt-1 text-xs text-fg-faint">
        JSON 형식으로 입력합니다. 저장할 때 형식을 확인합니다.
      </p>
    </div>
  )
}

export const WIDGETS: RegistryWidgetsType = {
  ...TEMPLATE_WIDGETS,
  template: TemplateWidget,
}

export const FIELDS: RegistryFieldsType = {
  jsonSchema: JsonSchemaField,
}
