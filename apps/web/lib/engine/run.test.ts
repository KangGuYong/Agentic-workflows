import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { startRun } from "./run"

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

beforeEach(() => {
  calls = []
})

afterEach(() => {
  vi.unstubAllGlobals()
})

const BODY = { inputs: { issue: "42" }, revision: 3 }

describe("startRun", () => {
  it("POSTs inputs and revision to the proxy", async () => {
    answer(reply(202, { runId: "r1", versionId: "v1" }))
    await startRun("wf_1", BODY)

    expect(calls[0]?.url).toBe("/api/engine/workflows/wf_1/runs")
    expect(calls[0]?.init?.method).toBe("POST")
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual(BODY)
  })

  it("reports the run it started", async () => {
    answer(reply(202, { runId: "r1", versionId: "v1" }))

    expect(await startRun("wf_1", BODY)).toEqual({ outcome: "started", runId: "r1", versionId: "v1" })
  })

  it("sends a different idempotency key each time", async () => {
    // The engine dedupes by this key, which is what makes a *retried* request safe. Reusing it would
    // collapse two deliberate runs into one: pressing 실행 twice on purpose must start two runs.
    answer(reply(202, { runId: "r1", versionId: "v1" }), reply(202, { runId: "r2", versionId: "v1" }))
    await startRun("wf_1", BODY)
    await startRun("wf_1", BODY)

    const keys = calls.map((call) => (call.init?.headers as Record<string, string>)["idempotency-key"])
    expect(keys[0]).toBeTruthy()
    expect(keys[0]).not.toBe(keys[1])
  })

  it("sends a key that is a UUID, not a counter", async () => {
    answer(reply(202, { runId: "r1", versionId: "v1" }))
    await startRun("wf_1", BODY)

    const key = (calls[0]?.init?.headers as Record<string, string>)["idempotency-key"]
    expect(key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)
  })

  it("fetches the other draft for a 409 that names only a revision", async () => {
    // Unlike the save endpoint's 409, this one carries no `draftDsl`. The conflict dialog offers a
    // choice between two drafts, so the other one has to be fetched or there is no choice to offer.
    const theirs = { version: "1", nodes: [], edges: [] }
    answer(
      reply(409, { error: { code: "REVISION_CONFLICT", message: "충돌", details: { currentRevision: 7 } } }),
      reply(200, { id: "wf_1", revision: 7, draftDsl: theirs }),
    )

    expect(await startRun("wf_1", BODY)).toEqual({ outcome: "stale", currentRevision: 7, draftDsl: theirs })
    expect(calls[1]?.url).toBe("/api/engine/workflows/wf_1")
  })

  it("still reports the conflict when the other draft cannot be fetched", async () => {
    // Without it the dialog can offer 덮어쓰기 but not 불러오기 -- better than pretending nothing is wrong.
    answer(
      reply(409, { error: { code: "REVISION_CONFLICT", details: { currentRevision: 7 } } }),
      reply(500, null),
    )

    expect(await startRun("wf_1", BODY)).toEqual({ outcome: "stale", currentRevision: 7, draftDsl: null })
  })

  it("treats a 409 with no revision as a plain failure", async () => {
    answer(reply(409, { error: { code: "REVISION_CONFLICT", message: "충돌" } }))

    expect(await startRun("wf_1", BODY)).toEqual({ outcome: "failed", message: "충돌" })
  })

  it("carries the issues out of a 422", async () => {
    // Validation can be stale: the editor's last `/validate` was clean and the engine's is not.
    const issues = [{ severity: "error", code: "INVALID_CONFIG", message: "설정 오류", nodeId: "llm_1" }]
    answer(reply(422, { error: { code: "VALIDATION_FAILED", message: "오류", details: { issues } } }))

    expect(await startRun("wf_1", BODY)).toEqual({ outcome: "rejected", issues })
  })

  it("passes the engine's message through on any other failure", async () => {
    answer(reply(413, { error: { code: "PAYLOAD_TOO_LARGE", message: "실행 입력이 너무 큽니다 (최대 64KB)" } }))

    expect(await startRun("wf_1", BODY)).toEqual({
      outcome: "failed",
      message: "실행 입력이 너무 큽니다 (최대 64KB)",
    })
  })

  it("names the status when the body is not JSON", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response("<html>502</html>", { status: 502 }))))

    expect(await startRun("wf_1", BODY)).toEqual({
      outcome: "failed",
      message: "실행을 시작하지 못했습니다 (502)",
    })
  })

  it("fails rather than guessing when a 202 body is not what the contract says", async () => {
    answer(reply(202, { runId: "r1" }))

    expect(await startRun("wf_1", BODY)).toMatchObject({ outcome: "failed" })
  })

  it("escapes the workflow id", async () => {
    answer(reply(202, { runId: "r1", versionId: "v1" }))
    await startRun("../secrets", BODY)

    expect(calls[0]?.url).toBe("/api/engine/workflows/..%2Fsecrets/runs")
  })
})
