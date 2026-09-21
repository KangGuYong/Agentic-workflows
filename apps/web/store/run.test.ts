import { describe, expect, it, vi } from "vitest"

import { emptyDsl } from "@/lib/dsl/document"
import type { RunResult } from "@/lib/engine/run"

import { createRunStore, type RunStore } from "./run"

function store(...answers: RunResult[]) {
  const queue = [...answers]
  const request = vi.fn(() => Promise.resolve(queue.shift() ?? { outcome: "failed" as const, message: "x" }))
  return { store: createRunStore(request), request }
}

async function started(s: RunStore) {
  await s.getState().start({ issue: "42" }, 3)
  return s.getState()
}

describe("starting a run", () => {
  it("keeps the run id a 202 reports", async () => {
    const { store: s } = store({ outcome: "started", runId: "r1", versionId: "v1" })

    expect((await started(s)).runId).toBe("r1")
    expect(s.getState().starting).toBe(false)
  })

  it("passes the inputs and the revision through", async () => {
    const { store: s, request } = store({ outcome: "started", runId: "r1", versionId: "v1" })
    await started(s)

    expect(request).toHaveBeenCalledWith({ inputs: { issue: "42" }, revision: 3 })
  })

  it("ignores a second press while the first is still in the air", async () => {
    // Right for a deliberate second run, wrong for an impatient double click on a slow network -- and
    // the store cannot tell them apart, so it takes the safer reading while one is pending.
    let release: (result: RunResult) => void = () => {}
    const request = vi.fn(() => new Promise<RunResult>((resolve) => (release = resolve)))
    const s = createRunStore(request)

    void s.getState().start({}, 1)
    void s.getState().start({}, 1)
    expect(request).toHaveBeenCalledTimes(1)

    release({ outcome: "started", runId: "r1", versionId: "v1" })
  })

  it("keeps the issues from a 422 instead of a bare message", async () => {
    // Validation can be stale: the editor's last `/validate` was clean and the engine's is not. The
    // issues go back on the canvas as badges, which is where they can be acted on.
    const issues = [{ severity: "error" as const, code: "INVALID_CONFIG", message: "설정 오류", nodeId: "llm_1" }]
    const { store: s } = store({ outcome: "rejected", issues })

    expect((await started(s)).rejected).toEqual(issues)
    expect(s.getState().runId).toBeNull()
  })

  it("keeps the conflict from a 409 for the dialog to resolve", async () => {
    const theirs = emptyDsl()
    const { store: s } = store({ outcome: "stale", currentRevision: 9, draftDsl: theirs })

    expect((await started(s)).stale).toEqual({ currentRevision: 9, draftDsl: theirs })
    expect(s.getState().runId).toBeNull()
  })

  it("keeps the engine's words on any other failure", async () => {
    const { store: s } = store({ outcome: "failed", message: "실행 입력이 너무 큽니다" })

    expect((await started(s)).error).toBe("실행 입력이 너무 큽니다")
  })

  it("treats a thrown request as a failure, not a crash", async () => {
    const s = createRunStore(() => Promise.reject(new Error("네트워크 오류")))
    await s.getState().start({}, 1)

    expect(s.getState().error).toBe("네트워크 오류")
    expect(s.getState().starting).toBe(false)
  })

  it("clears the last attempt's complaints before trying again", async () => {
    // Leaving the old badges up would make a successful retry look like it failed.
    const { store: s } = store(
      { outcome: "failed", message: "실패" },
      { outcome: "started", runId: "r1", versionId: "v1" },
    )
    await started(s)
    expect(s.getState().error).toBe("실패")

    await started(s)
    expect(s.getState().error).toBeNull()
    expect(s.getState().runId).toBe("r1")
  })

  it("lets the conflict be dismissed once it is dealt with", async () => {
    const { store: s } = store({ outcome: "stale", currentRevision: 9, draftDsl: null })
    await started(s)

    s.getState().clearStale()
    expect(s.getState().stale).toBeNull()
  })
})
