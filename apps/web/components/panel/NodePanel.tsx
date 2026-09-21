"use client"

import Form from "@rjsf/core"
import type { IChangeEvent } from "@rjsf/core"
import validator from "@rjsf/validator-ajv8"
import { useState } from "react"

import type { EditorNode, NodeConfig, Policy } from "@/lib/dsl/document"
import { effectivePolicy, policyOverride } from "@/lib/dsl/policy"
import { forcedSingleAttempt } from "@/lib/panel/retry"
import { buildUiSchema, collapseOptionalSchemas, hasTemplateField } from "@/lib/panel/uiSchema"
import type { NodeType } from "@/lib/palette"
import { templateContext } from "@/lib/template/context"
import type { GraphState } from "@/store/graph"
import { fieldOf, issuesForNode, type Issue } from "@/store/validation"

import { TEMPLATES } from "./templates"
import { IssueList } from "@/components/validation/Badge"

import { TemplateHelp } from "./TemplateHelp"
import { FIELDS, WIDGETS } from "./widgets"

/** The right-hand panel for the selected node (3 설계 §5.3, §5.4).
 *
 * Three tabs, and the middle one only exists for node types the engine gives a default policy -- there
 * is no timeout to override on a `template` node.
 */

type Tab = "settings" | "policy" | "label" | "trace"

export function NodePanel({
  node,
  nodeType,
  types = [],
  issues = [],
  trace,
  state,
}: {
  node: EditorNode
  nodeType: NodeType | undefined
  /** Every node type, for the labels the template editor shows in its completion list. */
  types?: readonly NodeType[]
  /** The whole `/validate` issue list; the panel picks out this node's. */
  issues?: readonly Issue[]
  /** The run history tab, present only while a run is being watched. */
  trace?: React.ReactNode
  state: GraphState
}) {
  const mine = issuesForNode(issues, node.id)
  const [tab, setTab] = useState<Tab>("settings")
  const hasPolicy = nodeType?.defaultPolicy != null
  const hasTrace = trace !== undefined
  // A tab that is no longer there cannot stay selected: the run ended, or this node type has no policy.
  const active = (tab === "policy" && !hasPolicy) || (tab === "trace" && !hasTrace) ? "settings" : tab

  return (
    <aside className="flex w-80 shrink-0 flex-col border-l border-ink-600 bg-ink-800">
      <header className="border-b border-ink-600 px-4 py-3">
        <p className="text-sm font-semibold">{node.label ?? nodeType?.label ?? node.type}</p>
        <p className="instrument-label mt-1">{node.id}</p>
      </header>

      <nav className="flex border-b border-ink-600" role="tablist">
        <Tabs active={active} hasPolicy={hasPolicy} hasTrace={hasTrace} onSelect={setTab} />
      </nav>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {active === "settings" ? (
          <Settings node={node} nodeType={nodeType} types={types} issues={mine} state={state} />
        ) : null}
        {active === "policy" && nodeType != null ? (
          <PolicyTab node={node} nodeType={nodeType} state={state} />
        ) : null}
        {active === "label" ? <LabelTab node={node} state={state} /> : null}
        {active === "trace" ? trace : null}
      </div>
    </aside>
  )
}

function Tabs({
  active,
  hasPolicy,
  hasTrace,
  onSelect,
}: {
  active: Tab
  hasPolicy: boolean
  hasTrace: boolean
  onSelect: (tab: Tab) => void
}) {
  const tabs: [Tab, string][] = [
    ["settings", "설정"],
    ...(hasPolicy ? ([["policy", "실행 정책"]] as [Tab, string][]) : []),
    ["label", "라벨"],
    // Last, and only during a run: it is a record of something that happened, not a thing to configure.
    ...(hasTrace ? ([["trace", "실행 기록"]] as [Tab, string][]) : []),
  ]
  return (
    <>
      {tabs.map(([id, label]) => (
        <button
          key={id}
          type="button"
          role="tab"
          aria-selected={active === id}
          onClick={() => onSelect(id)}
          className="border-b-2 px-4 py-2 text-sm"
          style={{
            borderColor: active === id ? "var(--accent)" : "transparent",
            color: active === id ? "var(--fg)" : "var(--fg-muted)",
          }}
        >
          {label}
        </button>
      ))}
    </>
  )
}

function Settings({
  node,
  nodeType,
  types,
  issues,
  state,
}: {
  node: EditorNode
  nodeType: NodeType | undefined
  types: readonly NodeType[]
  issues: readonly Issue[]
  state: GraphState
}) {
  if (nodeType === undefined) {
    return <p className="text-sm text-fg-muted">이 노드 타입의 설정 형식을 알 수 없습니다.</p>
  }
  // An issue whose `field` is `config.<path>` belongs beside that input; everything else belongs at the
  // top of the tab, where it is visible without hunting for which field it meant.
  const byField = new Map<string, Issue[]>()
  const general: Issue[] = []
  for (const issue of issues) {
    const field = fieldOf(issue)
    if (field === null) general.push(issue)
    else byField.set(field, [...(byField.get(field) ?? []), issue])
  }

  return (
    <>
      <IssueList issues={general} />
    <Form
      schema={collapseOptionalSchemas(nodeType)}
      uiSchema={buildUiSchema(nodeType)}
      formData={node.config ?? {}}
      validator={validator}
      widgets={WIDGETS}
      fields={FIELDS}
      templates={TEMPLATES}
      // The template editor needs the other nodes to complete against, which no part of this node's
      // schema describes. `null` analysis is honest: `/validate` is wired up in Task 13, and until then
      // the editor completes node ids without claiming anything about guarantees.
      formContext={{
        template: templateContext(state.dsl, types, null, node.id),
        issuesByField: byField,
      }}
      // RJSF's validation is display-only here: the engine's `/validate` is the authority, and blocking
      // an edit because a half-typed value does not match ajv would make the panel unusable.
      liveValidate={false}
      noHtml5Validate
      showErrorList={false}
      onChange={(event: IChangeEvent) => state.setNodeConfig(node.id, event.formData as NodeConfig)}
      onBlur={() => state.endEdit()}
    >
      {/* RJSF renders a submit button unless given children; there is nothing to submit. The template
          rules go here rather than under each template field: they are about the language, and two
          copies of them in a 320px panel is clutter. */}
      {hasTemplateField(nodeType) ? <TemplateHelp /> : <></>}
    </Form>
    </>
  )
}

function PolicyTab({
  node,
  nodeType,
  state,
}: {
  node: EditorNode
  nodeType: NodeType
  state: GraphState
}) {
  const base = (nodeType.defaultPolicy ?? null) as Policy | null
  const shown = effectivePolicy(node.policy, base)
  const forced = forcedSingleAttempt(node.type, node.config ?? {})

  function update(change: Policy) {
    state.setNodePolicy(node.id, policyOverride({ ...shown, ...change }, base))
  }

  return (
    <div className="flex flex-col gap-4">
      <Field id="policy-timeout" label="제한 시간 (초)">
        <input
          type="number"
          min={1}
          className="w-full border border-ink-600 bg-ink-700 px-2 py-1.5 text-sm outline-none focus:border-ink-500"
          style={{ borderRadius: "var(--radius)" }}
          id="policy-timeout"
          value={shown.timeoutSec ?? ""}
          onChange={(event) => update({ timeoutSec: Number(event.target.value) || undefined })}
          onBlur={() => state.endEdit()}
        />
      </Field>

      <Field id="policy-attempts" label="시도 횟수" help={forced ? "POST·PATCH는 재시도하지 않습니다. 같은 요청이 두 번 나가면 결제가 두 번 일어날 수 있습니다." : undefined}>
        <input
          type="number"
          min={1}
          max={10}
          id="policy-attempts"
          aria-describedby={forced ? "policy-attempts-help" : undefined}
          disabled={forced}
          className="w-full border border-ink-600 bg-ink-700 px-2 py-1.5 text-sm outline-none focus:border-ink-500 disabled:opacity-40"
          style={{ borderRadius: "var(--radius)" }}
          value={forced ? 1 : shown.retry?.maxAttempts ?? ""}
          onChange={(event) =>
            update({ retry: { ...shown.retry, maxAttempts: Number(event.target.value) || undefined } })
          }
          onBlur={() => state.endEdit()}
        />
      </Field>

      <Field id="policy-on-error" label="실패했을 때">
        <select
          className="w-full border border-ink-600 bg-ink-700 px-2 py-1.5 text-sm outline-none focus:border-ink-500"
          style={{ borderRadius: "var(--radius)" }}
          id="policy-on-error"
          value={shown.onError ?? "fail"}
          onChange={(event) => update({ onError: event.target.value as Policy["onError"] })}
        >
          <option value="fail">실행을 중단합니다</option>
          <option value="default">기본값으로 계속합니다</option>
        </select>
      </Field>

      <button
        type="button"
        onClick={() => state.setNodePolicy(node.id, undefined)}
        disabled={node.policy === undefined}
        className="self-start text-xs text-fg-muted underline disabled:opacity-35"
      >
        기본값으로 되돌리기
      </button>
    </div>
  )
}

function LabelTab({ node, state }: { node: EditorNode; state: GraphState }) {
  return (
    <Field id="node-label" label="라벨" help="캔버스에 보이는 이름입니다. 비우면 노드 타입 이름이 쓰입니다.">
      <input
        className="w-full border border-ink-600 bg-ink-700 px-2 py-1.5 text-sm outline-none focus:border-ink-500"
        style={{ borderRadius: "var(--radius)" }}
        id="node-label"
        aria-describedby="node-label-help"
        value={node.label ?? ""}
        onChange={(event) => state.setNodeLabel(node.id, event.target.value)}
        onBlur={() => state.endEdit()}
      />
    </Field>
  )
}

/** A labelled field.
 *
 * The help text sits *outside* the `<label>` and is linked with `aria-describedby`. Inside it, the
 * accessible name of the control becomes the label and the whole help sentence run together, so a
 * screen reader reads the entire explanation every time the field takes focus -- and
 * `getByLabelText("라벨")` cannot find it either, which is how this surfaced.
 */
function Field({
  id,
  label,
  help,
  children,
}: {
  id: string
  label: string
  help?: string
  children: React.ReactNode
}) {
  return (
    <div className="block">
      <label htmlFor={id} className="instrument-label mb-1 block">
        {label}
      </label>
      {children}
      {help !== undefined ? (
        <p id={`${id}-help`} className="mt-1 text-xs text-fg-faint">
          {help}
        </p>
      ) : null}
    </div>
  )
}
