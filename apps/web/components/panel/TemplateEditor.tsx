"use client"

import { autocompletion, type CompletionContext, type CompletionResult } from "@codemirror/autocomplete"
import { Compartment, EditorState, type Extension } from "@codemirror/state"
import {
  Decoration,
  EditorView,
  ViewPlugin,
  WidgetType,
  type DecorationSet,
  type ViewUpdate,
} from "@codemirror/view"
import { useEffect, useRef } from "react"

import { candidates, typedAt, type RefNode } from "@/lib/template/complete"
import type { TemplateContext } from "@/lib/template/context"
import { openReferenceAt, ranges } from "@/lib/template/parse"

/** The template field (3 설계 §6.1): autocomplete on `{{`, and references shown as label chips.
 *
 * CodeMirror rather than a textarea with an overlay. The overlay approach has to keep a second element
 * in sync with the real one across wrapping, scrolling and IME composition, and the Korean input this
 * editor exists for is exactly where that breaks.
 *
 * The view is created **once** and every change after that goes through a transaction. Recreating it on
 * a prop change would drop the in-flight IME composition and put the cursor back at the start -- which
 * is what a naive controlled wrapper does on every keystroke.
 */

/** A reference rendered as its node's label. */
class ChipWidget extends WidgetType {
  constructor(private readonly text: string) {
    super()
  }

  // Without this CodeMirror rebuilds every chip on each recompute, which drops a click mid-press.
  override eq(other: ChipWidget): boolean {
    return other.text === this.text
  }

  toDOM(): HTMLElement {
    const chip = document.createElement("span")
    chip.className = "cm-ref-chip"
    chip.textContent = this.text
    return chip
  }

  override ignoreEvent(): boolean {
    return false
  }
}

/** What a chip shows: the node's label and the field path below it.
 *
 * Falls back to the raw expression. A reference with a filter, an arithmetic expression or a node this
 * editor has never heard of is still a legal template -- showing it verbatim is honest, and hiding it
 * behind a guessed label would be worse than not chipping it at all.
 */
function chipText(inner: string, nodes: readonly RefNode[]): string {
  const simple = /^([A-Za-z_][A-Za-z0-9_]*)((?:\.[A-Za-z0-9_]+)*)$/.exec(inner)
  if (simple === null) return inner
  const node = nodes.find((candidate) => candidate.id === simple[1])
  return node === undefined ? inner : `${node.label}${simple[2] ?? ""}`
}

function chipsFor(view: EditorView, nodes: readonly RefNode[]): DecorationSet {
  const text = view.state.doc.toString()
  const { from, to } = view.state.selection.main
  const marks = []
  for (const range of ranges(text)) {
    // A chip the cursor is inside opens back into text. A reference that cannot be edited cannot be
    // corrected, and the first thing a person does with a wrong reference is click into it.
    if (to >= range.start && from <= range.end) continue
    marks.push(
      Decoration.replace({ widget: new ChipWidget(chipText(range.inner, nodes)) }).range(
        range.start,
        range.end,
      ),
    )
  }
  return Decoration.set(marks)
}

function chipPlugin(context: () => TemplateContext): Extension {
  return ViewPlugin.fromClass(
    class {
      decorations: DecorationSet

      constructor(view: EditorView) {
        this.decorations = chipsFor(view, context().nodes)
      }

      update(update: ViewUpdate) {
        // Selection as well as document: moving the cursor into a chip is what opens it.
        if (update.docChanged || update.selectionSet || update.viewportChanged) {
          this.decorations = chipsFor(update.view, context().nodes)
        }
      }
    },
    { decorations: (plugin) => plugin.decorations },
  )
}

function completionSource(context: () => TemplateContext) {
  return (completion: CompletionContext): CompletionResult | null => {
    const prefix = openReferenceAt(completion.state.doc.toString(), completion.pos)
    if (prefix === null) return null
    const typed = typedAt(prefix)
    if (typed === null) return null

    const { nodes, guaranteed } = context()
    const options = candidates(prefix, guaranteed, nodes)
    if (options.length === 0) return null
    return {
      from: completion.pos - typed.length,
      options: options.map((option) => ({
        label: option.label,
        // The document gets the id; the list shows the label. They are different on purpose.
        apply: option.insert,
        detail: option.detail,
        type: option.guaranteed === false ? "keyword" : "variable",
      })),
      // Korean labels are not word characters to CodeMirror's default matcher, so it would close the
      // popup on the first Hangul keystroke. `candidates` does the filtering.
      filter: false,
    }
  }
}

const THEME = EditorView.theme({
  "&": { fontSize: "13px", border: "1px solid var(--ink-600)", borderRadius: "var(--radius)" },
  "&.cm-focused": { outline: "none", borderColor: "var(--ink-500)" },
  ".cm-content": { fontFamily: "var(--font-mono)", padding: "6px 8px", caretColor: "var(--fg)" },
  ".cm-line": { padding: "0" },
  ".cm-ref-chip": {
    background: "color-mix(in srgb, var(--accent) 22%, transparent)",
    border: "1px solid color-mix(in srgb, var(--accent) 45%, transparent)",
    borderRadius: "3px",
    padding: "0 4px",
    whiteSpace: "nowrap",
  },
  ".cm-tooltip-autocomplete": {
    background: "var(--ink-700)",
    border: "1px solid var(--ink-600)",
    borderRadius: "var(--radius)",
  },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": { background: "var(--ink-600)", color: "var(--fg)" },
  ".cm-completionDetail": { color: "var(--fg-faint)", fontStyle: "normal", marginLeft: "0.75em" },
})

export function TemplateEditor({
  id,
  labelledBy,
  value,
  context,
  disabled = false,
  onChange,
  onBlur,
}: {
  id: string
  /** Id of the element that names this editor. CodeMirror's content is a `role=textbox` contenteditable,
   * which a `<label for>` cannot reach, so the name arrives through `aria-labelledby` instead. */
  labelledBy?: string
  value: string
  context: TemplateContext
  disabled?: boolean
  onChange: (value: string) => void
  onBlur: () => void
}) {
  const host = useRef<HTMLDivElement>(null)
  const view = useRef<EditorView>(null)
  /** `disabled` has to be swapped without rebuilding the editor, which is what a compartment is for. */
  const editable = useRef(new Compartment())
  // Read through a ref so the extensions, which are built once with the view, always see the current
  // props. Rebuilding them on every render would mean reconfiguring the editor on every keystroke.
  const latest = useRef({ context, onChange, onBlur })
  // In an effect, not during render: a ref written during render is lost under a re-entrant render, and
  // React's rules say so. The editor's callbacks all fire from user events, which are after the commit.
  useEffect(() => {
    latest.current = { context, onChange, onBlur }
  })

  useEffect(() => {
    const parent = host.current
    if (parent === null) return

    const editor = new EditorView({
      parent,
      state: EditorState.create({
        doc: value,
        extensions: [
          EditorView.lineWrapping,
          THEME,
          // Read once with the view, like everything else here: the id it derives from is fixed for the
          // life of the field, so there is nothing to reconfigure.
          EditorView.contentAttributes.of(labelledBy === undefined ? {} : { "aria-labelledby": labelledBy }),
          editable.current.of(EditorState.readOnly.of(disabled)),
          chipPlugin(() => latest.current.context),
          autocompletion({ override: [completionSource(() => latest.current.context)], icons: false }),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) latest.current.onChange(update.state.doc.toString())
            if (update.focusChanged && !update.view.hasFocus) latest.current.onBlur()
          }),
        ],
      }),
    })
    view.current = editor
    return () => {
      editor.destroy()
      view.current = null
    }
    // Deliberately once: `value` is applied by the effect below, through a transaction.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const editor = view.current
    if (editor === null) return
    const current = editor.state.doc.toString()
    // Only when it really differs, or the transaction we dispatch for our own `onChange` would move the
    // cursor to the end on every keystroke. Never mid-composition: replacing the document under an
    // active IME commits the half-formed syllable twice.
    if (current !== value && !editor.composing) {
      editor.dispatch({ changes: { from: 0, to: current.length, insert: value } })
    }
  }, [value])

  useEffect(() => {
    view.current?.dispatch({
      effects: editable.current.reconfigure(EditorState.readOnly.of(disabled)),
    })
  }, [disabled])

  return <div id={id} ref={host} data-testid="template-editor" className="cm-template" />
}
