import { describe, expect, it, vi } from "vitest"

import { emptyDsl } from "@/lib/dsl/document"
import type { NodeAnalysis } from "@/lib/template/context"

import {
  createValidationStore,
  errorCount,
  fieldOf,
  handleOf,
  issuesForEdge,
  issuesForNode,
  workflowIssues,
  worstOf,
  type Issue,
  type ValidationResponse,
} from "./validation"

const ANALYSIS: Record<string, NodeAnalysis> = {
  llm_1: { variables: ["start"], outputSchema: { type: "object" }, handles: [] },
}

function error(over: Partial<Issue> = {}): Issue {
  return { severity: "error", code: "INVALID_CONFIG", message: "설정 오류", ...over }
}

function warning(over: Partial<Issue> = {}): Issue {
  return { severity: "warning", code: "TYPE_WARNING", message: "경고", ...over }
}

function store(...answers: ValidationResponse[]) {
  const queue = [...answers]
  const validate = vi.fn(() => Promise.resolve(queue.shift() ?? { issues: [] }))
  return { store: createValidationStore(validate), validate }
}

describe("the validation slice", () => {
  it("takes the issues and the node analysis from a response", async () => {
    const { store: s } = store({ issues: [error()], nodes: ANALYSIS })
    await s.getState().validate(emptyDsl())

    expect(s.getState().issues).toHaveLength(1)
    expect(s.getState().nodes).toEqual(ANALYSIS)
    expect(s.getState().checking).toBe(false)
  })

  it("replaces the issues every time, including with none", async () => {
    const { store: s } = store({ issues: [error()], nodes: ANALYSIS }, { issues: [], nodes: ANALYSIS })
    await s.getState().validate(emptyDsl())
    await s.getState().validate(emptyDsl())

    expect(s.getState().issues).toEqual([])
  })

  it("keeps the previous analysis when a response carries none", async () => {
    // A structural error -- a duplicate node id, a cycle -- stops the walk before the analysis exists,
    // so `/validate` answers with issues and no `nodes`. Emptying the map there would empty the
    // template editor's completion list at exactly the moment someone is editing to fix the error.
    // Slightly stale schemas are a little wrong; an empty list is useless.
    const { store: s } = store({ issues: [], nodes: ANALYSIS }, { issues: [error()] })
    await s.getState().validate(emptyDsl())
    await s.getState().validate(emptyDsl())

    expect(s.getState().nodes).toEqual(ANALYSIS)
    expect(s.getState().issues).toHaveLength(1)
  })

  it("keeps it when the response carries an empty map, not just a missing one", async () => {
    const { store: s } = store({ issues: [], nodes: ANALYSIS }, { issues: [error()], nodes: {} })
    await s.getState().validate(emptyDsl())
    await s.getState().validate(emptyDsl())

    expect(s.getState().nodes).toEqual(ANALYSIS)
  })

  it("is null before any response has carried one", async () => {
    const { store: s } = store({ issues: [error()] })
    expect(s.getState().nodes).toBeNull()

    await s.getState().validate(emptyDsl())
    expect(s.getState().nodes).toBeNull()
  })

  it("keeps the issues when the request itself fails, and says it is unreachable", async () => {
    const validate = vi
      .fn()
      .mockResolvedValueOnce({ issues: [error()], nodes: ANALYSIS })
      .mockRejectedValueOnce(new Error("네트워크"))
    const s = createValidationStore(validate)

    await s.getState().validate(emptyDsl())
    await s.getState().validate(emptyDsl())

    // Clearing the badges because the engine is unreachable would say "no problems" when the truth is
    // "we do not know".
    expect(s.getState().issues).toHaveLength(1)
    expect(s.getState().unreachable).toBe(true)
    expect(s.getState().checking).toBe(false)
  })

  it("ignores an answer that arrives after a newer one", async () => {
    // Two requests in flight is the normal case while typing; the slow one must not win.
    let releaseFirst: (value: ValidationResponse) => void = () => {}
    const validate = vi
      .fn()
      .mockImplementationOnce(() => new Promise<ValidationResponse>((resolve) => (releaseFirst = resolve)))
      .mockResolvedValueOnce({ issues: [], nodes: ANALYSIS })
    const s = createValidationStore(validate)

    const slow = s.getState().validate(emptyDsl())
    await s.getState().validate(emptyDsl())
    releaseFirst({ issues: [error(), error()], nodes: {} })
    await slow

    expect(s.getState().issues).toEqual([])
  })
})

describe("reading the issues", () => {
  const ISSUES: Issue[] = [
    error({ nodeId: "llm_1", field: "config.model" }),
    warning({ nodeId: "llm_1" }),
    error({ edgeId: "e1" }),
    error({ code: "LIMIT_EXCEEDED", message: "워크플로가 너무 큽니다" }),
    error({ nodeId: "cond_1", code: "HANDLE_NOT_CONNECTED", field: "handles.yes" }),
  ]

  it("groups by node and by edge", () => {
    expect(issuesForNode(ISSUES, "llm_1")).toHaveLength(2)
    expect(issuesForEdge(ISSUES, "e1")).toHaveLength(1)
    expect(issuesForNode(ISSUES, "nobody")).toEqual([])
  })

  it("reports the worst severity, error over warning", () => {
    expect(worstOf(issuesForNode(ISSUES, "llm_1"))).toBe("error")
    expect(worstOf([warning()])).toBe("warning")
    expect(worstOf([])).toBeNull()
  })

  it("separates the issues that belong to no node", () => {
    // `LIMIT_EXCEEDED` is about the document's size. A badge on some arbitrary node would send someone
    // to fix the wrong thing.
    expect(workflowIssues(ISSUES).map((issue) => issue.code)).toEqual(["LIMIT_EXCEEDED"])
  })

  it("counts only the errors", () => {
    expect(errorCount(ISSUES)).toBe(4)
    expect(errorCount([warning(), warning()])).toBe(0)
  })

  it("reads the settings field an issue points at", () => {
    expect(fieldOf(ISSUES[0] as Issue)).toBe("model")
    expect(fieldOf(ISSUES[1] as Issue)).toBeNull()
  })

  it("reads the handle an issue points at, which is not a settings field", () => {
    const handle = ISSUES[4] as Issue
    expect(handleOf(handle)).toBe("yes")
    expect(fieldOf(handle)).toBeNull()
    expect(handleOf(ISSUES[0] as Issue)).toBeNull()
  })
})
