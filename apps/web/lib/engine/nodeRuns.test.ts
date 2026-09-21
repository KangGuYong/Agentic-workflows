import { afterEach, describe, expect, it, vi } from "vitest"

import { fetchNodeRuns } from "./nodeRuns"

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

describe("fetchNodeRuns", () => {
  it("asks the proxy for the run's rows", async () => {
    const fetchMock = reply(200, { nodeRuns: [], hasMore: false })
    vi.stubGlobal("fetch", fetchMock)
    await fetchNodeRuns("r1")

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/engine/runs/r1/nodes")
  })

  it("passes a limit when one is asked for", async () => {
    const fetchMock = reply(200, { nodeRuns: [], hasMore: false })
    vi.stubGlobal("fetch", fetchMock)
    await fetchNodeRuns("r1", { limit: 200 })

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/engine/runs/r1/nodes?limit=200")
  })

  it("escapes the run id", async () => {
    const fetchMock = reply(200, { nodeRuns: [], hasMore: false })
    vi.stubGlobal("fetch", fetchMock)
    await fetchNodeRuns("../secrets")

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/engine/runs/..%2Fsecrets/nodes")
  })

  it("carries hasMore through, which the engine knows rather than estimates", async () => {
    vi.stubGlobal("fetch", reply(200, { nodeRuns: [], hasMore: true }))

    expect((await fetchNodeRuns("r1")).hasMore).toBe(true)
  })

  it("treats a missing hasMore as false rather than as unknown", async () => {
    vi.stubGlobal("fetch", reply(200, { nodeRuns: [] }))

    expect((await fetchNodeRuns("r1")).hasMore).toBe(false)
  })

  it("throws on a non-200", async () => {
    vi.stubGlobal("fetch", reply(404, { error: { message: "없음" } }))

    await expect(fetchNodeRuns("r1")).rejects.toThrow(/404/)
  })

  it("throws rather than guessing when the body is not what the contract says", async () => {
    vi.stubGlobal("fetch", reply(200, { rows: [] }))

    await expect(fetchNodeRuns("r1")).rejects.toThrow()
  })
})
