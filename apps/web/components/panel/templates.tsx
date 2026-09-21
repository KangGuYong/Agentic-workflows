"use client"

import type {
  BaseInputTemplateProps,
  FieldTemplateProps,
  ObjectFieldTemplateProps,
  TemplatesType,
  WidgetProps,
} from "@rjsf/utils"

/** RJSF templates that make the generated form look like the rest of the panel.
 *
 * RJSF's defaults are deliberately unstyled HTML -- labels inline with controls, no spacing, a bare
 * checkbox. Next to the instrument-panel chrome that reads as a broken page rather than a plain one, so
 * the handful of slots that decide layout are replaced here. The widgets in `widgets.tsx` cover the two
 * field kinds RJSF cannot render at all; these cover how every field is framed.
 */

export const CONTROL =
  "w-full border border-ink-600 bg-ink-700 px-2 py-1.5 text-sm outline-none focus:border-ink-500 disabled:opacity-40"

function FieldTemplate({ id, label, required, children, description, errors, help, hidden, displayLabel }: FieldTemplateProps) {
  if (hidden) return <div className="hidden">{children}</div>
  return (
    <div className="mb-4">
      {displayLabel && label !== "" ? (
        <label htmlFor={id} className="instrument-label mb-1 block">
          {label}
          {required === true ? <span className="ml-1 text-st-failed">*</span> : null}
        </label>
      ) : null}
      {children}
      {description}
      {errors}
      {help}
    </div>
  )
}

function ObjectFieldTemplate({
  properties,
  title,
  description,
  fieldPathId,
  schema,
  disabled,
  readonly,
  onAddProperty,
}: ObjectFieldTemplateProps) {
  // The root object's title is pydantic's class name (`HttpRequestConfig`), which means nothing to a
  // user and duplicates the panel header. Nested objects keep theirs -- `headers` needs its heading.
  // RJSF 6 renamed `idSchema` to `fieldPathId`; the root still carries `$id === "root"`.
  const isRoot = fieldPathId.$id === "root"
  // A free-form map (`dict[str, str]` in the engine, `additionalProperties` here) is only usable if
  // there is a way to add a key. Dropping this button left `http_request` unable to set a header at
  // all -- which is most of what that node is for.
  const canAdd = schema.additionalProperties !== undefined && disabled !== true && readonly !== true

  return (
    <div>
      {!isRoot && title !== undefined && title !== "" ? (
        <p className="instrument-label mb-2">{title}</p>
      ) : null}
      {!isRoot && description !== undefined ? description : null}
      {properties.map((property) => (
        <div key={property.name}>{property.content}</div>
      ))}
      {canAdd ? (
        <button
          type="button"
          onClick={onAddProperty}
          className="border border-ink-600 bg-ink-700 px-2 py-1 text-xs hover:border-ink-500"
          style={{ borderRadius: "var(--radius)" }}
        >
          + 항목 추가
        </button>
      ) : null}
    </div>
  )
}

function BaseInputTemplate({
  id,
  value,
  type,
  required,
  disabled,
  readonly,
  autofocus,
  placeholder,
  onChange,
  onBlur,
  onFocus,
  options,
}: BaseInputTemplateProps) {
  return (
    <input
      id={id}
      type={type ?? "text"}
      className={CONTROL}
      style={{ borderRadius: "var(--radius)" }}
      value={value ?? ""}
      required={required}
      disabled={disabled === true || readonly === true}
      autoFocus={autofocus}
      placeholder={placeholder}
      list={options?.enumOptions !== undefined ? `${id}-list` : undefined}
      onChange={(event) => onChange(event.target.value === "" ? options?.emptyValue : event.target.value)}
      onBlur={(event) => onBlur(id, event.target.value)}
      onFocus={(event) => onFocus(id, event.target.value)}
    />
  )
}

function SelectWidget({ id, value, required, disabled, readonly, options, onChange, onBlur }: WidgetProps) {
  const choices = options.enumOptions ?? []
  return (
    <select
      id={id}
      className={CONTROL}
      style={{ borderRadius: "var(--radius)" }}
      value={value ?? ""}
      required={required}
      disabled={disabled === true || readonly === true}
      onChange={(event) => onChange(event.target.value === "" ? undefined : event.target.value)}
      onBlur={(event) => onBlur(id, event.target.value)}
    >
      {/* An optional enum needs an empty row, or the first value looks deliberately chosen. */}
      {required === true ? null : <option value="">선택 안 함</option>}
      {choices.map((choice) => (
        <option key={String(choice.value)} value={String(choice.value)}>
          {choice.label}
        </option>
      ))}
    </select>
  )
}

function CheckboxWidget({ id, value, disabled, readonly, label, onChange }: WidgetProps) {
  return (
    <label htmlFor={id} className="flex items-center gap-2 text-sm">
      <input
        id={id}
        type="checkbox"
        className="size-4 accent-[var(--accent)]"
        checked={value === true}
        disabled={disabled === true || readonly === true}
        onChange={(event) => onChange(event.target.checked)}
      />
      {label}
    </label>
  )
}

export const TEMPLATES: Partial<TemplatesType> = {
  FieldTemplate,
  ObjectFieldTemplate,
  BaseInputTemplate,
}

export const TEMPLATE_WIDGETS = {
  SelectWidget,
  CheckboxWidget,
}
