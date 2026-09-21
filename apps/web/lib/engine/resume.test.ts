import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { cancelRun, resumeRun, type ResumeBody } from "./resume"

function reply(status: number, body: unknown) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  })
}

let calls: { url: string; init?: RequestInit }[]

function answer(...responses: Response[]) {
  const queue = [...responses]
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init })
      return Promise.resolve(queue.shift() ?? reply(500, null))
    }),
  )
}

function sent() {
  return JSON.parse(String(calls[0]?.init?.body)) as Record<string, unknown>
}

const BODY: ResumeBody = { nodeId: "approval_1", execIndex: 1, decision: "approve" }

beforeEach(() => {
  calls = []
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("resumeRun", () => {
  it("POSTs the answer to the proxy", async () => {
    answer(reply(202, { status: "queued" }))
    await resumeRun("r1", BODY)

    expect(calls[0]?.url).toBe("/api/engine/runs/r1/resume")
    expect(calls[0]?.init?.method).toBe("POST")
  })

  it("does not send reviewedAt", async () => {
    // The engine discards a client-supplied value before validation ever sees it: a node has no way to
    // tell a forged timestamp from a real one. Sending one would suggest this browser's clock decides
    // when a review happened.
    answer(reply(202, { status: "queued" }))
    await resumeRun("r1", { ...BODY, comment: "좋습니다" })

    expect(Object.keys(sent())).not.toContain("reviewedAt")
  })

  it("drops a reviewedAt a caller supplies anyway", async () => {
    // The version above passes for the wrong reason: `ResumeBody` has no such field, so a well-typed
    // caller never provides one and the allowlist is never exercised. This is the guarantee that
    // actually matters -- the allowlist, not the type, is what keeps it off the wire.
    answer(reply(202, { status: "queued" }))
    await resumeRun("r1", { ...BODY, reviewedAt: "1999-01-01T00:00:00Z" } as ResumeBody)

    expect(Object.keys(sent())).not.toContain("reviewedAt")
    expect(JSON.stringify(sent())).not.toContain("1999")
  })

  it("sends exactly the keys the engine allows", async () => {
    answer(reply(202, { status: "queued" }))
    await resumeRun("r1", { ...BODY, comment: "좋습니다", editedValue: { text: "고친 값" } })

    expect(Object.keys(sent()).sort()).toEqual(["comment", "decision", "editedValue", "execIndex", "nodeId"])
  })

  it("leaves an absent optional out rather than sending null", async () => {
    // `resume_output` rejects an answer with an unknown key, and a null comment is not the same as none.
    answer(reply(202, { status: "queued" }))
    await resumeRun("r1", BODY)

    expect(Object.keys(sent()).sort()).toEqual(["decision", "execIndex", "nodeId"])
  })

  it("names the approval it answers", async () => {
    // The engine 409s an answer that does not match the waiting target.
    answer(reply(202, { status: "queued" }))
    await resumeRun("r1", { ...BODY, execIndex: 3 })

    expect(sent()).toMatchObject({ nodeId: "approval_1", execIndex: 3, decision: "approve" })
  })

  it("carries a rejection through as the decision, not as a failure", async () => {
    answer(reply(202, { status: "queued" }))
    const result = await resumeRun("r1", { ...BODY, decision: "reject", comment: "내용이 부족합니다" })

    expect(result).toEqual({ outcome: "queued" })
    expect(sent()["decision"]).toBe("reject")
  })

  it("reports a 409 as stale, whichever of the three it is", async () => {
    // RESUME_TARGET_MISMATCH, INVALID_STATE_TRANSITION and RUN_DATA_EXPIRED all mean the same thing to
    // a reviewer: this approval is no longer theirs to answer.
    for (const code of ["RESUME_TARGET_MISMATCH", "INVALID_STATE_TRANSITION", "RUN_DATA_EXPIRED"]) {
      calls = []
      answer(reply(409, { error: { code, message: "대기 중인 승인과 대상이 다릅니다" } }))
      expect(await resumeRun("r1", BODY), code).toEqual({
        outcome: "stale",
        message: "대기 중인 승인과 대상이 다릅니다",
      })
    }
  })

  it("passes a 422's message through, which is already Korean", async () => {
    answer(reply(422, { error: { code: "VALIDATION_FAILED", message: "잘못된 승인 응답입니다: 객체가 아닙니다" } }))

    expect(await resumeRun("r1", BODY)).toEqual({
      outcome: "rejected",
      message: "잘못된 승인 응답입니다: 객체가 아닙니다",
    })
  })

  it("names the status when there is nothing to pass through", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response("<html>502</html>", { status: 502 }))))

    expect(await resumeRun("r1", BODY)).toEqual({ outcome: "failed", message: "승인을 보내지 못했습니다 (502)" })
  })

  it("escapes the run id", async () => {
    answer(reply(202, { status: "queued" }))
    await resumeRun("../secrets", BODY)

    expect(calls[0]?.url).toBe("/api/engine/runs/..%2Fsecrets/resume")
  })
})

describe("cancelRun", () => {
  it("POSTs to the proxy", async () => {
    answer(reply(202, { status: "cancelled" }))
    await cancelRun("r1")

    expect(calls[0]?.url).toBe("/api/engine/runs/r1/cancel")
    expect(calls[0]?.init?.method).toBe("POST")
  })

  it("reports a run the API ended outright", async () => {
    // A queued or parked run has no worker holding it, so the API cancels it then and there.
    answer(reply(202, { status: "cancelled" }))

    expect(await cancelRun("r1")).toEqual({ outcome: "cancelled" })
  })

  it("reports a running run as merely requested", async () => {
    // A worker holds it; it stops at its next heartbeat, and the UI waits for `run_cancelled`.
    answer(reply(202, { status: "running" }))

    expect(await cancelRun("r1")).toEqual({ outcome: "requested" })
  })

  it("treats an unrecognised status as requested rather than as done", async () => {
    // Saying "cancelled" when it is not is the worse mistake: the run keeps going and the UI says it
    // stopped.
    answer(reply(202, { status: "확실하지 않음" }))

    expect(await cancelRun("r1")).toEqual({ outcome: "requested" })
  })

  it("passes the engine's message through when it refuses", async () => {
    answer(reply(409, { error: { code: "INVALID_STATE_TRANSITION", message: "이미 끝난 실행입니다" } }))

    expect(await cancelRun("r1")).toEqual({ outcome: "failed", message: "이미 끝난 실행입니다" })
  })

  it("escapes the run id", async () => {
    answer(reply(202, { status: "cancelled" }))
    await cancelRun("../secrets")

    expect(calls[0]?.url).toBe("/api/engine/runs/..%2Fsecrets/cancel")
  })
})
