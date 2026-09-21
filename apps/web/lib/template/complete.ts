import { kindNames, kindsOf, propertiesOf, resolvePath, type JsonSchema } from "./schema"

/** What to offer at the cursor inside a `{{ }}` (3 설계 §6.1).
 *
 * Pure, and takes everything it needs as arguments: the editor's autocomplete source is one line on top
 * of this, and every rule below is a rule about the engine's reference semantics rather than about
 * CodeMirror.
 */

/** One node the editor may offer, as `/validate` describes it plus the label only the document knows. */
export interface RefNode {
  id: string
  label: string
  outputSchema: JsonSchema
}

export interface Candidate {
  /** What goes into the document. A node **id**, or a property name -- never a label. */
  insert: string
  /** What the list shows: the node's label, because that is the name the person gave it. */
  label: string
  /** The grey right-hand text: the value's kind, and any warning that applies. */
  detail: string
  /** Whether the engine guarantees this node has run by here. `null` before validation has answered. */
  guaranteed: boolean | null
}

/** The engine refuses to resolve a reference to `end` (`refs.py::_check_ref` -> REF_UNKNOWN_NODE). */
const NOT_REFERENCEABLE = new Set(["end"])

/** The path immediately left of the cursor: the start of the prefix, or after whitespace.
 *
 * Unicode letters, not `[A-Za-z_]`. Node ids are ASCII, but the list is filtered by **label** too, and
 * the labels are Korean -- someone looking for 요약 types 요. An ASCII-only token stops matching at the
 * first Hangul character, so the popup closes on the keystroke that should have narrowed it.
 */
const PATH_AT_CURSOR = /(?:^|\s)([\p{L}_][\p{L}\p{N}_]*(?:\.[\p{L}\p{N}_]*)*|)$/u

/** Positions inside a `{{ }}` where what follows is not a reference.
 *
 * After a `|` the next word is a filter name; inside a `(` it is a filter argument -- `default("")`
 * takes a literal, and the engine forbids calls outside the filters it allows. Offering node ids in
 * either place puts an identifier where the engine will reject one.
 */
const NOT_A_REFERENCE = /[|(]/

/** Whether the cursor sits inside an unterminated string literal in `prefix`. */
function inString(prefix: string): boolean {
  let quote: string | undefined
  for (let index = 0; index < prefix.length; index += 1) {
    const char = prefix[index]
    if (quote !== undefined) {
      if (char === "\\") index += 1
      else if (char === quote) quote = undefined
      continue
    }
    if (char === '"' || char === "'") quote = char
  }
  return quote !== undefined
}

function detailFor(schema: JsonSchema | undefined, suffix?: string): string {
  const kinds = [...kindsOf(schema)].map((kind) => kindNames[kind] ?? kind).join(", ")
  return suffix === undefined ? kinds : `${kinds} · ${suffix}`
}

function matches(text: string, typed: string): boolean {
  return text.toLowerCase().includes(typed.toLowerCase())
}

function nodeCandidates(
  typed: string,
  guaranteed: readonly string[] | null,
  nodes: readonly RefNode[],
): Candidate[] {
  const certain = guaranteed === null ? null : new Set(guaranteed)
  return nodes
    .filter((node) => !NOT_REFERENCEABLE.has(node.id))
    .filter((node) => typed === "" || matches(node.id, typed) || matches(node.label, typed))
    .map((node) => {
      // `null` is "validation has not answered yet", not "not guaranteed". Marking everything
      // "기본값 필요" in that state would be a warning about nothing.
      const isCertain = certain === null ? null : certain.has(node.id)
      return {
        insert: node.id,
        label: node.label,
        detail: isCertain === false ? `${node.id} · 기본값 필요` : node.id,
        guaranteed: isCertain,
      }
    })
}

function fieldCandidates(
  root: string,
  path: readonly string[],
  typed: string,
  nodes: readonly RefNode[],
): Candidate[] {
  const node = nodes.find((candidate) => candidate.id === root)
  if (node === undefined || NOT_REFERENCEABLE.has(node.id)) return []

  const at = resolvePath(node.outputSchema, path)
  const properties = propertiesOf(at)
  const exact = properties.find((property) => property.name === typed)
  // A finished reference. `{{ llm_1.text }}` cannot go deeper, so a popup here would only re-offer the
  // word already typed -- and take the next keypress to dismiss.
  if (exact !== undefined && propertiesOf(exact.schema).length === 0) return []

  return properties
    .filter((property) => typed === "" || matches(property.name, typed))
    .map((property) => ({
      insert: property.name,
      label: property.name,
      detail: detailFor(property.schema, property.required ? undefined : "없을 수 있음"),
      // A property of a node that is offered at all belongs to that node's guarantee, not its own.
      guaranteed: null,
    }))
}

/**
 * @param prefix   the text between the opening `{{` and the cursor
 * @param variables the node ids the engine guarantees have run (`/validate` `nodes[x].variables`), or
 *                  `null` while validation has not answered
 * @param nodes    every node that could be referenced, with the output schema `/validate` reported
 */
/** The token the cursor is in the middle of, or `null` where completion does not apply.
 *
 * The editor needs its length to know what a chosen candidate replaces: picking 요약 after typing `요`
 * has to overwrite that `요`, not append `llm_1` to it. Same rules as `candidates`, so the two cannot
 * disagree about where the token starts.
 */
export function typedAt(prefix: string): string | null {
  if (inString(prefix) || NOT_A_REFERENCE.test(prefix)) return null
  const match = PATH_AT_CURSOR.exec(prefix)
  if (match === null) return null
  const segments = (match[1] ?? "").split(".")
  return segments[segments.length - 1] ?? ""
}

export function candidates(
  prefix: string,
  variables: readonly string[] | null,
  nodes: readonly RefNode[],
): Candidate[] {
  if (inString(prefix) || NOT_A_REFERENCE.test(prefix)) return []
  const match = PATH_AT_CURSOR.exec(prefix)
  if (match === null) return []

  const segments = (match[1] ?? "").split(".")
  const typed = segments[segments.length - 1] ?? ""
  if (segments.length === 1) return nodeCandidates(typed, variables, nodes)
  return fieldCandidates(segments[0] ?? "", segments.slice(1, -1), typed, nodes)
}
