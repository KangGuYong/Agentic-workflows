import { describe, expect, it } from "vitest"

import { applyEvent, emptyStream, type RunEvent } from "./events"
import { isTerminalStatus, restoreStream, shouldStream, type RunView } from "./restore"
import type { NodeRun } from "./trace"

function view(over: Partial<RunView> = {}): RunView {
  return { id: "r1", status: "running", cancelRequested: false, ...over }
}

function nodeRun(over: Partial<NodeRun> = {}): NodeRun {
  return {
    nodeId: "llm_1",
    execIndex: 1,
    attempt: 1,
    status: "succeeded",
    input: null,
    output: null,
    error: null,
    meta: null,
    truncated: false,
    tokensIn: 0,
    tokensOut: 0,
    startedAt: "2026-09-21T04:00:00.000Z",
    finishedAt: "2026-09-21T04:00:01.000Z",
    ...over,
  }
}

describe("restoreStream", () => {
  it("takes the run's status", () => {
    for (const [status, expected] of [
      ["queued", "queued"],
      ["running", "running"],
      ["waiting", "waiting"],
      ["succeeded", "succeeded"],
      ["failed", "failed"],
      ["cancelled", "cancelled"],
    ] as const) {
      expect(restoreStream(view({ status }), []).status, status).toBe(expected)
    }
  })

  it("treats a status it does not know as queued rather than crashing", () => {
    // A newer engine could add one. Queued is the reading that claims the least.
    expect(restoreStream(view({ status: "something_new" }), []).status).toBe("queued")
  })

  it("is finished exactly for the three terminal statuses", () => {
    expect(restoreStream(view({ status: "succeeded" }), []).finished).toBe(true)
    expect(restoreStream(view({ status: "failed" }), []).finished).toBe(true)
    expect(restoreStream(view({ status: "cancelled" }), []).finished).toBe(true)
    expect(restoreStream(view({ status: "waiting" }), []).finished).toBe(false)
    expect(restoreStream(view({ status: "running" }), []).finished).toBe(false)
  })

  it("paints each node from its latest attempt, in either row order", () => {
    // The rows are per attempt; what a canvas shows is where each node stands now. The attempts
    // themselves are read one by one in the trace panel.
    const first = nodeRun({ attempt: 1, status: "failed" })
    const second = nodeRun({ attempt: 2, status: "succeeded" })

    expect(restoreStream(view(), [first, second]).nodes["llm_1"]).toMatchObject({ status: "succeeded", attempt: 2 })
    expect(restoreStream(view(), [second, first]).nodes["llm_1"]).toMatchObject({ status: "succeeded", attempt: 2 })
  })

  it("prefers the latest iteration over the highest attempt number", () => {
    // Both orderings, because the rows arrive in whatever order the API returns them and the first
    // version of this test only happened to list the winner first.
    const later = nodeRun({ execIndex: 2, attempt: 1, status: "running" })
    const earlier = nodeRun({ execIndex: 1, attempt: 5, status: "succeeded" })

    expect(restoreStream(view(), [later, earlier]).nodes["llm_1"]).toMatchObject({
      status: "running",
      attempt: 1,
    })
    expect(restoreStream(view(), [earlier, later]).nodes["llm_1"]).toMatchObject({
      status: "running",
      attempt: 1,
    })
  })

  it("keeps each node separate", () => {
    const state = restoreStream(view(), [nodeRun(), nodeRun({ nodeId: "http_1", status: "failed" })])

    expect(Object.keys(state.nodes).sort()).toEqual(["http_1", "llm_1"])
  })

  it("carries the error of a failed attempt", () => {
    const state = restoreStream(view(), [
      nodeRun({ status: "failed", error: { code: "HTTP_BLOCKED", message: "차단된 요청입니다" } }),
    ])

    expect(state.nodes["llm_1"]?.error).toEqual({ code: "HTTP_BLOCKED", message: "차단된 요청입니다" })
  })

  it("restores no tokens, because the engine never stored any", () => {
    // Transient by design (설계 7.3): they carry no `seq` and are not written. Inventing text here
    // would be worse than showing none.
    const state = restoreStream(view(), [nodeRun({ status: "running", finishedAt: null })])

    expect(state.nodes["llm_1"]?.tokens).toBe("")
  })

  it("marks a defaulted node as such", () => {
    const state = restoreStream(view(), [nodeRun({ status: "defaulted" })])

    expect(state.nodes["llm_1"]).toMatchObject({ status: "defaulted", defaulted: true })
  })

  it("restores the approval a parked run is waiting on", () => {
    const state = restoreStream(
      view({ status: "waiting", waitingFor: { nodeId: "approval_1", execIndex: 1, message: "확인", allowEdit: false } }),
      [],
    )

    expect(state.waitingFor).toEqual({
      nodeId: "approval_1",
      execIndex: 1,
      message: "확인",
      review: undefined,
      allowEdit: false,
    })
  })

  it("has no approval when the run is not parked on one", () => {
    expect(restoreStream(view(), []).waitingFor).toBeNull()
    expect(restoreStream(view({ waitingFor: { message: "확인" } }), []).waitingFor).toBeNull()
  })

  it("restores a cancel that was asked for and has not landed", () => {
    expect(restoreStream(view({ cancelRequested: true }), []).cancelling).toBe(true)
  })

  it("does not say cancelling for a run that already ended", () => {
    // Nothing is left to cancel, and 취소 중 next to 취소됨 reads as though it is still stopping.
    expect(restoreStream(view({ status: "cancelled", cancelRequested: true }), []).cancelling).toBe(false)
  })

  it("starts with a live connection and no disconnection", () => {
    expect(restoreStream(view(), [])).toMatchObject({ disconnected: false, lastSeq: null })
  })
})

describe("shouldStream", () => {
  it("is false for a run that already ended", () => {
    // Opening one would replay every stored event to arrive at the state already on screen, and
    // `EventSource` would then reconnect to a stream the server closes at once, forever.
    expect(shouldStream(restoreStream(view({ status: "succeeded" }), []))).toBe(false)
  })

  it("is true while the run can still change", () => {
    for (const status of ["queued", "running", "waiting"]) {
      expect(shouldStream(restoreStream(view({ status }), [])), status).toBe(true)
    }
  })
})

describe("isTerminalStatus", () => {
  it("names the three that end a run", () => {
    expect(["succeeded", "failed", "cancelled"].every(isTerminalStatus)).toBe(true)
    expect(["queued", "running", "waiting", "무엇"].some(isTerminalStatus)).toBe(false)
  })
})

describe("replaying stored events onto restored state", () => {
  it("leaves one node, not two rows of state", () => {
    // The engine replays what it stored when a stream opens from the beginning. The per-node state has
    // to be idempotent under that, or a reconnect would double everything it had already painted.
    const events: RunEvent[] = [
      { type: "run_started", seq: 1 },
      { type: "node_started", seq: 2, nodeId: "llm_1", attempt: 1 },
      { type: "node_finished", seq: 3, nodeId: "llm_1", attempt: 1 },
    ]
    const once = events.reduce(applyEvent, emptyStream())
    const twice = events.reduce(applyEvent, once)

    expect(Object.keys(twice.nodes)).toEqual(["llm_1"])
    expect(twice.nodes["llm_1"]).toMatchObject({ status: "succeeded", attempt: 1 })
    expect(twice).toEqual(once)
  })

  it("arrives at the same place from restored state as from nothing", () => {
    const restored = restoreStream(view(), [nodeRun({ status: "succeeded" })])
    const events: RunEvent[] = [
      { type: "node_started", seq: 2, nodeId: "llm_1", attempt: 1 },
      { type: "node_finished", seq: 3, nodeId: "llm_1", attempt: 1 },
    ]

    expect(events.reduce(applyEvent, restored).nodes["llm_1"]).toMatchObject({
      status: "succeeded",
      attempt: 1,
    })
  })
})
