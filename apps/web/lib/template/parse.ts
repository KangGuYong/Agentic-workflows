/** Finding the `{{ ... }}` references in a template field (3 설계 §6.1).
 *
 * A scanner, not a regex. The editor needs *offsets* -- a chip decoration is a range in the document --
 * and it needs them on a document that is being typed into, where `{{` is unclosed most of the time.
 * A regex over the whole text would also read a `}}` inside a string literal as the end of the
 * reference, which puts the chip over half the expression.
 *
 * This is deliberately a lexer and nothing more: it does not parse the expression inside. The engine's
 * `engine/templates/parser.py` is the authority on what a reference means, and duplicating its Jinja
 * analysis here would give the editor a second opinion that drifts.
 */

export interface TemplateRange {
  /** Offset of the opening `{{`. */
  start: number
  /** Offset just past the closing `}}`, so `text.slice(start, end)` is the whole reference. */
  end: number
  /** The expression between the delimiters, trimmed, without the `-` whitespace-control markers. */
  inner: string
}

/** Jinja's three block kinds. Only `{{ }}` produces a range; the other two are skipped whole. */
const OPENERS: [open: string, close: string, isReference: boolean][] = [
  ["{#", "#}", false],
  ["{%", "%}", false],
  ["{{", "}}", true],
]

/** Where the `{{` opened at `from` closes, or -1 if it does not.
 *
 * Skips over string literals, because `default("}}")` is a template a person will write. Jinja's own
 * lexer does the same; without it the range ends inside the literal. */
function closeOf(text: string, from: number, close: string): number {
  let index = from
  while (index < text.length) {
    const char = text[index]
    if (char === '"' || char === "'") {
      index = skipString(text, index, char)
      continue
    }
    if (text.startsWith(close, index)) return index
    index += 1
  }
  return -1
}

/** Past the closing quote of the string starting at `from`, or past the end if it never closes. */
function skipString(text: string, from: number, quote: string): number {
  let index = from + 1
  while (index < text.length) {
    if (text[index] === "\\") {
      index += 2
      continue
    }
    if (text[index] === quote) return index + 1
    index += 1
  }
  return text.length
}

function trimInner(raw: string): string {
  let inner = raw
  if (inner.startsWith("-")) inner = inner.slice(1)
  if (inner.endsWith("-")) inner = inner.slice(0, -1)
  return inner.trim()
}

export function ranges(text: string): TemplateRange[] {
  const found: TemplateRange[] = []
  let index = 0

  while (index < text.length) {
    const opener = OPENERS.find(([open]) => text.startsWith(open, index))
    if (opener === undefined) {
      index += 1
      continue
    }
    const [open, close, isReference] = opener
    // A comment or statement is opaque: a `}}` inside one is not the end of anything, and neither is a
    // `{{`. Searching for the *block's own* close delimiter is what makes that true.
    const at = isReference
      ? closeOf(text, index + open.length, close)
      : text.indexOf(close, index + open.length)
    if (at === -1) {
      // Unclosed. Everything after it is inside the unfinished block, so there is nothing more to find
      // -- and while a person is typing `{{`, this is the usual state.
      break
    }
    if (isReference) {
      found.push({ start: index, end: at + close.length, inner: trimInner(text.slice(index + open.length, at)) })
    }
    index = at + close.length
  }
  return found
}

/** The text between the `{{` the cursor is inside and the cursor itself, or `null` if it is not inside
 * one.
 *
 * Not expressible with `ranges`: while a person types `{{ llm`, the reference has no closing `}}` yet,
 * so it is not a range. This is the state autocomplete runs in almost every time.
 */
export function openReferenceAt(text: string, pos: number): string | null {
  const before = text.slice(0, pos)
  const open = before.lastIndexOf("{{")
  if (open === -1) return null
  const prefix = before.slice(open + 2)
  // A `}}` between the brace and the cursor means that reference already ended; `%}` or `#}` mean the
  // `{{` was inside a block and the cursor is past it. Either way the cursor is outside.
  if (/\}\}|%\}|#\}/.test(prefix)) return null
  // `{# {{ x` -- the brace is inside a comment that never closed, so it is text, not a reference.
  const openComment = before.lastIndexOf("{#")
  const openBlock = before.lastIndexOf("{%")
  if (openComment > open || openBlock > open) return null
  if (openComment !== -1 && openComment < open && !before.slice(openComment).includes("#}")) return null
  if (openBlock !== -1 && openBlock < open && !before.slice(openBlock).includes("%}")) return null
  return prefix
}
