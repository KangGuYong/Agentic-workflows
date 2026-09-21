"use client"

import type { FieldProps, RegistryFieldsType, RegistryWidgetsType, WidgetProps } from "@rjsf/utils"

import type { TemplateContext } from "@/lib/template/context"
import type { JsonSchema } from "@/lib/template/schema"

import { TEMPLATE_WIDGETS } from "./templates"
import { SchemaEditor } from "./SchemaEditor"
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

/** The schema field for `start.inputs` and `llm.outputSchema`.
 *
 * A **field**, not a widget. The engine declares these as `anyOf: [{type: object}, {type: null}]` --
 * optional -- and RJSF renders any `anyOf` with its own option selector before it ever looks at
 * `ui:widget`. Registered as a widget, this never appeared: the panel showed a bare number input
 * holding the selected branch index instead. (`collapseOptionalSchemas` removes the selector too; the
 * field is what puts the editor there.)
 */
export function JsonSchemaField({ schema, fieldPathId, formData, disabled, readonly, onChange, uiSchema }: FieldProps) {
  const id = fieldPathId.$id
  const title = uiSchema?.["ui:title"] ?? schema.title ?? ""

  return (
    <fieldset className="mb-4">
      {title === "" ? null : <legend className="instrument-label mb-1">{title}</legend>}
      <SchemaEditor
        id={id}
        value={formData === null ? undefined : (formData as JsonSchema | undefined)}
        disabled={disabled === true || readonly === true}
        // A field's `onChange` carries the path, unlike a widget's: it writes into the form data itself
        // rather than into one already-located slot.
        onChange={(next) => onChange(next, fieldPathId.path)}
      />
    </fieldset>
  )
}

export const WIDGETS: RegistryWidgetsType = {
  ...TEMPLATE_WIDGETS,
  template: TemplateWidget,
}

export const FIELDS: RegistryFieldsType = {
  jsonSchema: JsonSchemaField,
}
