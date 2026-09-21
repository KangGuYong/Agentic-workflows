"use client"

import { useState } from "react"

import {
  addField,
  canOpen,
  duplicateNames,
  fieldsAt,
  itemTypeOf,
  losesFields,
  removeField,
  setField,
  setFieldType,
  setItemType,
  trail,
  type Path,
} from "@/lib/schema/edit"
import {
  emptyModel,
  FIELD_TYPES,
  toModel,
  toSchema,
  TYPE_NAMES,
  type FieldModel,
  type FieldType,
  type SchemaModel,
} from "@/lib/schema/model"
import type { JsonSchema } from "@/lib/template/schema"

/** The editor for `start.inputs` and `llm.outputSchema` (3 설계 §7).
 *
 * A form by default, because the people this is for do not know JSON Schema. A raw JSON mode beside it,
 * because the people who do should not have to fight a table.
 *
 * The rule that decides everything awkward here: **a schema the form cannot represent is never
 * rewritten.** The toggle to 폼 is disabled, it says why, and the JSON stays exactly as the tenant
 * wrote it. Validation is `/validate`'s job (Task 13); this component never reimplements
 * `engine/jsondata.py::schema_problems`.
 */

const CONTROL =
  "w-full border border-ink-600 bg-ink-700 px-2 py-1 text-xs outline-none focus:border-ink-500 disabled:opacity-40"

/** Whether two schemas are the same document, for deciding if an incoming `value` is really new. */
function sameSchema(a: JsonSchema | undefined, b: JsonSchema | undefined): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null)
}

export function SchemaEditor({
  id,
  value,
  disabled = false,
  onChange,
}: {
  id: string
  value: JsonSchema | undefined
  disabled?: boolean
  onChange: (schema: JsonSchema | undefined) => void
}) {
  // The model is **state**, not a value derived from the prop on every render.
  //
  // Deriving it was wrong in a way that only showed up under a test: the model holds things a JSON
  // Schema cannot -- two fields with the same name, a field whose name is still empty because someone
  // is typing it. Round-tripping through `toSchema` on every keystroke destroys them. Renaming a field
  // to a name already in use would have silently deleted it, mid-keystroke, with no way to notice.
  //
  // `mirror` is the schema this model corresponds to, so the parent echoing our own output back is not
  // mistaken for an external change. A genuinely new schema -- an undo, a different node -- resets it.
  const [state, setState] = useState<{ model: SchemaModel | null; mirror: JsonSchema | undefined }>(() => ({
    model: toModel(value),
    mirror: value,
  }))
  if (!sameSchema(value, state.mirror)) {
    // Adjusting state during render rather than in an effect: React re-runs this component before
    // committing, so no frame is ever painted against the stale model.
    setState({ model: toModel(value), mirror: value })
  }
  const model = state.model
  const representable = model !== null
  const [mode, setMode] = useState<"form" | "json">("form")
  const [path, setPath] = useState<Path>([])

  function change(next: SchemaModel) {
    const schema = toSchema(next)
    setState({ model: next, mirror: schema })
    onChange(schema)
  }

  // A schema that arrived from elsewhere -- an undo, another node -- can be one the form cannot show.
  // Falling back to JSON is the only honest answer; silently showing an empty table would not be.
  const shown = representable ? mode : "json"
  // Validated on the way out rather than reset in an effect. A stored path outlives what it pointed at
  // -- an undo, or a type change on the row above it -- and deriving the usable one during render means
  // there is never a frame rendered against a path that does not resolve.
  const here: Path = model !== null && fieldsAt(model, path) !== null ? path : []

  return (
    <div>
      <div className="mb-2 flex items-center gap-1">
        <Toggle active={shown === "form"} disabled={!representable} onClick={() => setMode("form")}>
          폼
        </Toggle>
        <Toggle active={shown === "json"} disabled={false} onClick={() => setMode("json")}>
          JSON
        </Toggle>
      </div>

      {representable ? null : (
        <p className="mb-2 text-xs text-fg-faint">
          이 형식에는 폼으로 다룰 수 없는 항목이 있어 JSON으로 표시합니다. 내용은 그대로 유지됩니다.
        </p>
      )}

      {shown === "form" && model !== null ? (
        <FormMode
          id={id}
          model={model}
          path={here}
          disabled={disabled}
          onPath={setPath}
          onChange={change}
        />
      ) : (
        <JsonMode id={id} value={value} disabled={disabled} onChange={onChange} />
      )}
    </div>
  )
}

function Toggle({
  active,
  disabled,
  onClick,
  children,
}: {
  active: boolean
  disabled: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-pressed={active}
      className="border px-2 py-0.5 text-xs disabled:opacity-35"
      style={{
        borderRadius: "var(--radius)",
        borderColor: active ? "var(--accent)" : "var(--ink-600)",
        color: active ? "var(--fg)" : "var(--fg-muted)",
      }}
    >
      {children}
    </button>
  )
}

function FormMode({
  id,
  model,
  path,
  disabled,
  onPath,
  onChange,
}: {
  id: string
  model: SchemaModel
  path: Path
  disabled: boolean
  onPath: (path: Path) => void
  onChange: (model: SchemaModel) => void
}) {
  // Always resolves: `SchemaEditor` validated the path before handing it over.
  const fields = fieldsAt(model, path) ?? []
  const names = trail(model, path)
  const duplicates = duplicateNames(fields)

  return (
    <div>
      <Breadcrumb names={names} onPath={onPath} />

      {path.length === 0 ? (
        <label className="mb-3 block">
          <span className="instrument-label mb-1 block">설명 (선택)</span>
          <input
            id={`${id}-description`}
            className={CONTROL}
            style={{ borderRadius: "var(--radius)" }}
            value={model.description}
            disabled={disabled}
            onChange={(event) => onChange({ ...model, description: event.target.value })}
          />
        </label>
      ) : null}

      {fields.length === 0 ? (
        <p className="mb-2 text-xs text-fg-faint">아직 필드가 없습니다.</p>
      ) : (
        <ul className="mb-2 flex flex-col gap-3">
          {fields.map((field, index) => (
            <Row
              key={index}
              id={`${id}-${path.join("-")}-${index}`}
              field={field}
              duplicate={duplicates.has(field.name)}
              disabled={disabled}
              onOpen={() => onPath([...path, index])}
              onRemove={() => onChange(removeField(model, path, index))}
              onField={(change) => onChange(setField(model, path, index, change))}
              onType={(type) => onChange(setFieldType(model, path, index, type))}
              onItemType={(type) => onChange(setItemType(model, path, index, type))}
            />
          ))}
        </ul>
      )}

      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange(addField(model, path))}
        className="border border-ink-600 bg-ink-700 px-2 py-1 text-xs hover:border-ink-500 disabled:opacity-40"
        style={{ borderRadius: "var(--radius)" }}
      >
        + 필드 추가
      </button>
    </div>
  )
}

function Breadcrumb({ names, onPath }: { names: string[]; onPath: (path: Path) => void }) {
  if (names.length === 0) return null
  return (
    <nav aria-label="위치" className="mb-2 flex flex-wrap items-center gap-1 text-xs text-fg-muted">
      <button type="button" onClick={() => onPath([])} className="underline">
        전체
      </button>
      {names.map((name, index) => (
        <span key={index} className="flex items-center gap-1">
          <span aria-hidden>/</span>
          {index === names.length - 1 ? (
            <span className="text-fg">{name}</span>
          ) : (
            <button type="button" onClick={() => onPath(Array.from({ length: index + 1 }, (_, i) => i))} className="underline">
              {name}
            </button>
          )}
        </span>
      ))}
    </nav>
  )
}

function Row({
  id,
  field,
  duplicate,
  disabled,
  onOpen,
  onRemove,
  onField,
  onType,
  onItemType,
}: {
  id: string
  field: FieldModel
  duplicate: boolean
  disabled: boolean
  onOpen: () => void
  onRemove: () => void
  onField: (change: Partial<Pick<FieldModel, "name" | "required" | "description">>) => void
  onType: (type: FieldType) => void
  onItemType: (type: FieldType) => void
}) {
  const itemType = itemTypeOf(field.value)

  function changeType(next: FieldType) {
    // Asking beats discarding. `losesFields` is false for 객체 <-> 목록, which keeps its contents, so
    // this only interrupts a change that really throws something away.
    if (losesFields(field, next) && !window.confirm("하위 항목이 함께 삭제됩니다. 계속할까요?")) return
    onType(next)
  }

  return (
    <li className="border border-ink-600 p-2" style={{ borderRadius: "var(--radius)" }}>
      <div className="flex items-start gap-2">
        <label className="min-w-0 flex-1">
          <span className="instrument-label mb-1 block">이름</span>
          <input
            id={`${id}-name`}
            className={CONTROL}
            style={{ borderRadius: "var(--radius)" }}
            value={field.name}
            disabled={disabled}
            aria-invalid={duplicate}
            onChange={(event) => onField({ name: event.target.value })}
          />
        </label>
        <label className="w-24 shrink-0">
          <span className="instrument-label mb-1 block">형식</span>
          <select
            id={`${id}-type`}
            className={CONTROL}
            style={{ borderRadius: "var(--radius)" }}
            value={field.value.kind}
            disabled={disabled}
            onChange={(event) => changeType(event.target.value as FieldType)}
          >
            {FIELD_TYPES.map((type) => (
              <option key={type} value={type}>
                {TYPE_NAMES[type]}
              </option>
            ))}
          </select>
        </label>
      </div>

      {itemType === null ? null : (
        <label className="mt-2 block">
          <span className="instrument-label mb-1 block">목록 항목 형식</span>
          <select
            id={`${id}-item-type`}
            className={CONTROL}
            style={{ borderRadius: "var(--radius)" }}
            value={itemType}
            disabled={disabled}
            onChange={(event) => onItemType(event.target.value as FieldType)}
          >
            {FIELD_TYPES.map((type) => (
              <option key={type} value={type}>
                {TYPE_NAMES[type]}
              </option>
            ))}
          </select>
        </label>
      )}

      <label className="mt-2 block">
        <span className="instrument-label mb-1 block">설명 (선택)</span>
        <input
          id={`${id}-description`}
          className={CONTROL}
          style={{ borderRadius: "var(--radius)" }}
          value={field.description}
          disabled={disabled}
          onChange={(event) => onField({ description: event.target.value })}
        />
      </label>

      {duplicate ? (
        <p className="mt-1 text-xs text-st-failed">
          이름이 중복됩니다. 저장하면 마지막 항목만 남습니다.
        </p>
      ) : null}

      <div className="mt-2 flex items-center justify-between">
        <label className="flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            className="size-3.5 accent-[var(--accent)]"
            checked={field.required}
            disabled={disabled}
            onChange={(event) => onField({ required: event.target.checked })}
          />
          필수
        </label>
        <div className="flex items-center gap-2">
          {canOpen(field) ? (
            <button type="button" onClick={onOpen} disabled={disabled} className="text-xs text-fg-muted underline disabled:opacity-40">
              하위 항목
            </button>
          ) : null}
          <button type="button" onClick={onRemove} disabled={disabled} className="text-xs text-fg-muted underline disabled:opacity-40">
            삭제
          </button>
        </div>
      </div>
    </li>
  )
}

function JsonMode({
  id,
  value,
  disabled,
  onChange,
}: {
  id: string
  value: JsonSchema | undefined
  disabled: boolean
  onChange: (schema: JsonSchema | undefined) => void
}) {
  const text = value === undefined || value === null ? "" : JSON.stringify(value, null, 2)
  const [invalid, setInvalid] = useState(false)

  return (
    <div>
      <textarea
        id={id}
        rows={8}
        aria-describedby={`${id}-help`}
        className={`${CONTROL} font-mono`}
        style={{ borderRadius: "var(--radius)" }}
        // Uncontrolled between blurs: half-typed JSON is invalid JSON, and re-serializing the parsed
        // value on every keystroke would fight whoever is typing it.
        defaultValue={text}
        // Remounts when the schema changes underneath -- an undo, or a switch back from the form --
        // which an uncontrolled textarea would otherwise ignore.
        key={text}
        disabled={disabled}
        onBlur={(event) => {
          const raw = event.target.value.trim()
          if (raw === "") {
            setInvalid(false)
            onChange(undefined)
            return
          }
          try {
            onChange(JSON.parse(raw) as JsonSchema)
            setInvalid(false)
          } catch {
            // Left exactly as typed. Saying so is the whole response: the engine's `/validate` decides
            // whether a *parseable* schema is usable, but nothing can be done with text that is not JSON.
            setInvalid(true)
          }
        }}
      />
      <p id={`${id}-help`} className="mt-1 text-xs text-fg-faint">
        JSON 형식으로 입력합니다. 저장할 때 엔진이 형식을 확인합니다.
      </p>
      {invalid ? <p className="mt-1 text-xs text-st-failed">JSON 형식이 아닙니다. 입력한 내용은 그대로 두었습니다.</p> : null}
    </div>
  )
}

export { emptyModel }
