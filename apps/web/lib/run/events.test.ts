import { describe, expect, it } from "vitest"

import {
  applyEvent,
  emptyStream,
  markCancelling,
  markDisconnected,
  TERMINAL_EVENTS,
  type RunEvent,
  type StreamState,
} from "./events"

function play(...events: RunEvent[]): StreamState {
  return events.reduce(applyEvent, emptyStream())
}

const STARTED: RunEvent = { type: "node_started", seq: 2, nodeId: "llm_1", execIndex: 1, attempt: 1 }

describe("run status", () => {
  it("starts queued and follows the run's own events", () => {
    expect(emptyStream().status).toBe("queued")
    expect(play({ type: "run_started", seq: 1 }).status).toBe("running")
    expect(play({ type: "run_started", seq: 1 }, { type: "run_waiting", seq: 2 }).status).toBe("waiting")
  })

  it("treats resume and recovery as running again", () => {
    expect(play({ type: "run_resumed", seq: 1 }).status).toBe("running")
    expect(play({ type: "run_recovered", seq: 1 }).status).toBe("running")
  })

  it("is finished on each terminal event, and only on those", () => {
    for (const type of TERMINAL_EVENTS) {
      expect(play({ type, seq: 9 }).finished, type).toBe(true)
    }
    expect(play({ type: "run_waiting", seq: 9 }).finished).toBe(false)
  })

  it("ignores everything after a terminal event", () => {
    // A replayed frame, or a stream that should have closed. Acting on it would move a finished run
    // back to running, which is a thing the UI cannot recover from on its own.
    const after = play({ type: "run_succeeded", seq: 9 }, { type: "run_started", seq: 10 }, STARTED)

    expect(after.status).toBe("succeeded")
    expect(after.nodes).toEqual({})
  })
})

describe("node status", () => {
  it("marks a node running, then succeeded", () => {
    const state = play(STARTED, { type: "node_finished", seq: 3, nodeId: "llm_1", attempt: 1 })

    expect(state.nodes["llm_1"]).toMatchObject({ status: "succeeded", attempt: 1 })
  })

  it("distinguishes a defaulted finish from a real one", () => {
    // `onError: "default"` means the node produced the fallback, not its own answer. Showing it as a
    // plain success hides that the workflow ran on a stand-in value.
    const state = play(STARTED, { type: "node_finished", seq: 3, nodeId: "llm_1", defaulted: true })

    expect(state.nodes["llm_1"]).toMatchObject({ status: "defaulted", defaulted: true })
  })

  it("keeps a node running while the engine will retry it", () => {
    // Showing 실패 here is a lie the next `node_started` corrects a moment later -- a flicker that reads
    // as a bug in the editor rather than as a retry.
    const state = play(STARTED, {
      type: "node_failed",
      seq: 3,
      nodeId: "llm_1",
      willRetry: true,
      error: { code: "TIMEOUT", message: "시간 초과" },
    })

    expect(state.nodes["llm_1"]?.status).toBe("running")
    expect(state.nodes["llm_1"]?.error).toEqual({ code: "TIMEOUT", message: "시간 초과" })
  })

  it("marks it failed once the engine gives up", () => {
    const state = play(STARTED, { type: "node_failed", seq: 3, nodeId: "llm_1", willRetry: false })

    expect(state.nodes["llm_1"]?.status).toBe("failed")
  })

  it("marks a node waiting for a person", () => {
    const state = play(STARTED, { type: "node_waiting", seq: 3, nodeId: "approval_1", attempt: 1 })

    expect(state.nodes["approval_1"]?.status).toBe("waiting")
  })

  it("clears the previous attempt's tokens and error when a retry starts", () => {
    // They belong to the attempt that failed. Leaving them shows the old failure under a node that is
    // running again.
    const state = play(
      STARTED,
      { type: "node_token", nodeId: "llm_1", text: "처음" },
      { type: "node_failed", seq: 3, nodeId: "llm_1", willRetry: true, error: { code: "TIMEOUT" } },
      { type: "node_started", seq: 4, nodeId: "llm_1", attempt: 2 },
    )

    expect(state.nodes["llm_1"]).toMatchObject({ status: "running", attempt: 2, tokens: "", error: null })
  })

  it("ignores a node event with no node id", () => {
    expect(play({ type: "node_started", seq: 2 }).nodes).toEqual({})
  })
})

describe("tokens", () => {
  it("appends in order", () => {
    const state = play(
      STARTED,
      { type: "node_token", nodeId: "llm_1", text: "안녕" },
      { type: "node_token", nodeId: "llm_1", text: "하세요" },
    )

    expect(state.nodes["llm_1"]?.tokens).toBe("안녕하세요")
  })

  it("never changes a node's status", () => {
    // A token is output, not a state transition. Letting it set "running" would resurrect a node that
    // had already finished -- which is exactly what a late token after `node_finished` would do.
    const state = play(
      STARTED,
      { type: "node_finished", seq: 3, nodeId: "llm_1" },
      { type: "node_token", nodeId: "llm_1", text: "늦은 토큰" },
    )

    expect(state.nodes["llm_1"]?.status).toBe("succeeded")
    expect(state.nodes["llm_1"]?.tokens).toBe("늦은 토큰")
  })

  it("treats a token with no text as nothing to append", () => {
    const state = play(STARTED, { type: "node_token", nodeId: "llm_1" })

    expect(state.nodes["llm_1"]?.tokens).toBe("")
  })
})

describe("the resume point", () => {
  it("follows the engine's ids", () => {
    expect(play({ type: "run_started", seq: 1 }, STARTED).lastSeq).toBe(2)
  })

  it("is not moved backwards by a transient event", () => {
    // `node_token` carries no id (design 7.3). Treating its absence as 0 would make a reconnect replay
    // the whole run from the beginning.
    const state = play({ type: "run_started", seq: 5 }, { type: "node_token", nodeId: "llm_1", text: "x" })

    expect(state.lastSeq).toBe(5)
  })

  it("is not moved backwards by an out-of-order id", () => {
    expect(play({ type: "run_started", seq: 5 }, { type: "node_started", seq: 3, nodeId: "a" }).lastSeq).toBe(5)
  })

  it("is null before anything with an id arrives", () => {
    expect(play({ type: "node_token", nodeId: "llm_1", text: "x" }).lastSeq).toBeNull()
  })
})

describe("an unknown event type", () => {
  it("is ignored without breaking the stream", () => {
    // A newer engine may send one. The right response is to keep the connection and drop the frame.
    const state = play({ type: "run_started", seq: 1 }, { type: "something_new", seq: 2, nodeId: "llm_1" })

    expect(state.status).toBe("running")
    expect(state.nodes).toEqual({})
    expect(state.lastSeq).toBe(2)
  })
})

describe("disconnection", () => {
  it("is recorded while the run is still going", () => {
    expect(markDisconnected(play({ type: "run_started", seq: 1 })).disconnected).toBe(true)
  })

  it("is not recorded for a run that already finished", () => {
    // The stream closing after a terminal event is the stream working, not a connection problem.
    expect(markDisconnected(play({ type: "run_succeeded", seq: 9 })).disconnected).toBe(false)
  })

  it("is cleared by the next message of any kind", () => {
    const dropped = markDisconnected(play({ type: "run_started", seq: 1 }))

    expect(applyEvent(dropped, { type: "node_token", nodeId: "llm_1", text: "x" }).disconnected).toBe(false)
  })
})

describe("the approval a run is parked on", () => {
  const WAITING: RunEvent = {
    type: "run_waiting",
    seq: 5,
    payload: { nodeId: "approval_1", execIndex: 1, message: "이 요약을 승인해 주세요", review: { text: "요약" }, allowEdit: true },
  }

  it("is read out of the run_waiting payload", () => {
    expect(play({ type: "run_started", seq: 1 }, WAITING).waitingFor).toEqual({
      nodeId: "approval_1",
      execIndex: 1,
      message: "이 요약을 승인해 주세요",
      review: { text: "요약" },
      allowEdit: true,
    })
  })

  it("is null before anything parks", () => {
    expect(emptyStream().waitingFor).toBeNull()
  })

  it("is cleared when the run moves on", () => {
    // A resumed run is no longer parked, and the approval that payload described cannot be answered.
    expect(play({ type: "run_started", seq: 1 }, WAITING, { type: "run_resumed", seq: 6 }).waitingFor).toBeNull()
  })

  it("is cleared when the run ends", () => {
    expect(play(WAITING, { type: "run_cancelled", seq: 6 }).waitingFor).toBeNull()
  })

  it("is null for a payload with no target to answer", () => {
    // `resume` is addressed by (nodeId, execIndex); without them there is nothing to send.
    for (const payload of [undefined, null, {}, { nodeId: "a" }, { execIndex: 1 }, { nodeId: 1, execIndex: 1 }]) {
      expect(play({ type: "run_waiting", seq: 5, payload }).waitingFor, JSON.stringify(payload)).toBeNull()
    }
  })

  it("defaults allowEdit to false, which is the safer reading", () => {
    const state = play({ type: "run_waiting", seq: 5, payload: { nodeId: "a", execIndex: 1, message: "확인" } })

    expect(state.waitingFor?.allowEdit).toBe(false)
  })
})

describe("cancelling", () => {
  it("is recorded when asked for", () => {
    expect(markCancelling(play({ type: "run_started", seq: 1 })).cancelling).toBe(true)
  })

  it("is not recorded for a run that already ended", () => {
    expect(markCancelling(play({ type: "run_succeeded", seq: 9 })).cancelling).toBe(false)
  })

  it("is cleared only by the run actually ending", () => {
    // Clearing it on a timer would tell someone the run stopped while the worker may still be
    // finishing a node.
    const asked = markCancelling(play({ type: "run_started", seq: 1 }))

    const stillGoing = applyEvent(asked, { type: "node_finished", seq: 2, nodeId: "llm_1" })
    expect(stillGoing.cancelling).toBe(true)

    expect(applyEvent(stillGoing, { type: "run_cancelled", seq: 3 }).cancelling).toBe(false)
  })

  it("is cleared by any ending, not only by a cancellation", () => {
    // A run that finished on its own while the cancel was in flight is over either way.
    const asked = markCancelling(play({ type: "run_started", seq: 1 }))

    expect(applyEvent(asked, { type: "run_succeeded", seq: 2 }).cancelling).toBe(false)
  })
})
