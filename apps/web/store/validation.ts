import { createStore, type StoreApi } from "zustand/vanilla"

import type { EditorDsl } from "@/lib/dsl/document"
import type { NodeAnalysis } from "@/lib/template/context"

/** The last thing `/validate` said (3 설계 §4.1).
 *
 * Two pieces with different lifetimes, and the difference is the whole point of this slice.
 *
 * `issues` is what to show *now*: it is replaced every time, including with nothing.
 *
 * `nodes` is what autocomplete reads, and it is **kept when a response has none**. A workflow with a
 * structural error -- a duplicate node id, a cycle -- never reaches the analysis that produces it, so
 * `/validate` answers with issues and an empty `nodes`. Replacing the map there would empty the
 * template editor's completion list at exactly the moment someone is editing to fix the error. The
 * stale schemas are a little wrong; an empty list is useless.
 */

export type Severity = "error" | "warning"

/** One entry of `/validate`'s `issues` (engine `validator/issues.py`). */
export interface Issue {
  severity: Severity
  code: string
  message: string
  nodeId?: string
  edgeId?: string
  /** `config.<path>` for a settings field, `handles.<name>` for a branch output. */
  field?: string
}

export interface ValidationResponse {
  issues: Issue[]
  nodes?: Record<string, NodeAnalysis>
}

export type ValidateRequest = (dsl: EditorDsl) => Promise<ValidationResponse>

export interface ValidationState {
  issues: Issue[]
  /** Per-node analysis, or `null` before the first response that carried one. */
  nodes: Record<string, NodeAnalysis> | null
  /** True while a request is out, so the badges can be shown as possibly stale. */
  checking: boolean
  /** Set when the request itself failed; the issues from before are kept. */
  unreachable: boolean

  validate: (dsl: EditorDsl) => Promise<void>
}

export type ValidationStore = StoreApi<ValidationState>

export function createValidationStore(validate: ValidateRequest): ValidationStore {
  return createStore<ValidationState>((set, get) => {
    /** Answers can land out of order; only the newest may write. */
    let latest = 0

    return {
      issues: [],
      nodes: null,
      checking: false,
      unreachable: false,

      async validate(dsl) {
        const ticket = (latest += 1)
        set({ checking: true })
        let response: ValidationResponse
        try {
          response = await validate(dsl)
        } catch {
          if (ticket === latest) set({ checking: false, unreachable: true })
          return
        }
        if (ticket !== latest) return
        set({
          issues: response.issues,
          // Kept, not replaced, when the response carries none. See the note above.
          nodes: response.nodes === undefined || Object.keys(response.nodes).length === 0
            ? get().nodes
            : response.nodes,
          checking: false,
          unreachable: false,
        })
      },
    }
  })
}

/** Issues that belong to one node, including the ones addressed to its fields and handles. */
export function issuesForNode(issues: readonly Issue[], nodeId: string): Issue[] {
  return issues.filter((issue) => issue.nodeId === nodeId)
}

export function issuesForEdge(issues: readonly Issue[], edgeId: string): Issue[] {
  return issues.filter((issue) => issue.edgeId === edgeId)
}

/** The worst severity among some issues, or `null` when there are none. */
export function worstOf(issues: readonly Issue[]): Severity | null {
  if (issues.some((issue) => issue.severity === "error")) return "error"
  return issues.some((issue) => issue.severity === "warning") ? "warning" : null
}

/** Issues about the workflow as a whole rather than any one node or edge.
 *
 * `LIMIT_EXCEEDED` is the case that matters: it is about the document's size, so there is no node to
 * put a badge on, and a badge on an arbitrary one would send someone to fix the wrong thing.
 */
export function workflowIssues(issues: readonly Issue[]): Issue[] {
  return issues.filter((issue) => issue.nodeId === undefined && issue.edgeId === undefined)
}

export function errorCount(issues: readonly Issue[]): number {
  return issues.filter((issue) => issue.severity === "error").length
}

/** The field path a settings input is addressed by, e.g. `config.url` -> `url`. */
export function fieldOf(issue: Issue): string | null {
  return issue.field?.startsWith("config.") === true ? issue.field.slice("config.".length) : null
}

/** The handle a `HANDLE_NOT_CONNECTED` names, e.g. `handles.yes` -> `yes`. */
export function handleOf(issue: Issue): string | null {
  return issue.field?.startsWith("handles.") === true ? issue.field.slice("handles.".length) : null
}
