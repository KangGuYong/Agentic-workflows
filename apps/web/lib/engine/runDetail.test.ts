import { afterEach, describe, expect, it, vi } from "vitest"

import { fetchRun } from "./runDetail"

function reply(status: number, body: unknown) {
  return vi.fn((_url: string, _init?: RequestInit) =>
    Promise.resolve(
      new Response(body === undefined ? null : JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
    ),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("fetchRun", () => {
  it("asks the proxy for the run", async () => {
    const fetchMock = reply(200, { id: "r1", status: "running", cancelRequested: false })
    vi.stubGlobal("fetch", fetchMock)
    await fetchRun("r1")

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/engine/runs/r1")
  })

  it("escapes the run id", async () => {
    const fetchMock = reply(200, { id: "x", status: "running" })
    vi.stubGlobal("fetch", fetchMock)
    await fetchRun("../secrets")

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/engine/runs/..%2Fsecrets")
  })

  it("reports the run", async () => {
    vi.stubGlobal(
      "fetch",
      reply(200, {
        id: "r1",
        status: "waiting",
        cancelRequested: true,
        waitingFor: { nodeId: "a", execIndex: 1, message: "확인", allowEdit: false },
      }),
    )

    expect(await fetchRun("r1")).toEqual({
      outcome: "run",
      run: {
        id: "r1",
        status: "waiting",
        cancelRequested: true,
        waitingFor: { nodeId: "a", execIndex: 1, message: "확인", allowEdit: false },
        error: undefined,
      },
    })
  })

  it("tells a run that is gone apart from a failure", async () => {
    // `?run=` survives in a bookmarked URL and the run it names can be deleted. Clearing the parameter
    // is right for that and wrong for an unreachable engine.
    vi.stubGlobal("fetch", reply(404, { error: { message: "실행을 찾을 수 없습니다" } }))

    expect(await fetchRun("r1")).toEqual({ outcome: "gone" })
  })

  it("reports a transport failure as one", async () => {
    vi.stubGlobal("fetch", reply(502, null))

    expect(await fetchRun("r1")).toMatchObject({ outcome: "failed" })
  })

  it("reports a thrown request as a failure, not as a missing run", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("네트워크 오류"))))

    expect(await fetchRun("r1")).toEqual({ outcome: "failed", message: "네트워크 오류" })
  })

  it("fails rather than guessing when the body has no status", async () => {
    vi.stubGlobal("fetch", reply(200, { id: "r1" }))

    expect(await fetchRun("r1")).toMatchObject({ outcome: "failed" })
  })

  it("treats a missing cancelRequested as false", async () => {
    vi.stubGlobal("fetch", reply(200, { id: "r1", status: "running" }))

    expect(await fetchRun("r1")).toMatchObject({ outcome: "run", run: { cancelRequested: false } })
  })
})
