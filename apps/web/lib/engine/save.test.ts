import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { emptyDsl } from "@/lib/dsl/document"

import { saveDraft } from "./save"

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

beforeEach(() => {
  vi.stubGlobal("fetch", reply(200, { revision: 2 }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

const BODY = { draftDsl: emptyDsl(), revision: 1 }

describe("saveDraft", () => {
  it("PUTs the draft to the proxy, not to the engine directly", async () => {
    const fetchMock = reply(200, { revision: 2 })
    vi.stubGlobal("fetch", fetchMock)
    await saveDraft("wf_1", BODY)

    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(url).toBe("/api/engine/workflows/wf_1")
    expect(init?.method).toBe("PUT")
    // The token lives on the server; the browser never holds one (3 설계 §3.2).
    expect(JSON.stringify(init?.headers)).not.toMatch(/authorization/i)
  })

  it("escapes the workflow id rather than pasting it into the path", async () => {
    const fetchMock = reply(200, { revision: 2 })
    vi.stubGlobal("fetch", fetchMock)
    await saveDraft("../secrets", BODY)

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/engine/workflows/..%2Fsecrets")
  })

  it("reports the new revision on 200", async () => {
    expect(await saveDraft("wf_1", BODY)).toEqual({ outcome: "saved", revision: 2 })
  })

  it("fails rather than guessing when the 200 body is not what the contract says", async () => {
    vi.stubGlobal("fetch", reply(200, { ok: true }))

    expect(await saveDraft("wf_1", BODY)).toMatchObject({ outcome: "failed" })
  })

  it("reads the other side's draft out of a 409", async () => {
    const theirs = emptyDsl()
    vi.stubGlobal(
      "fetch",
      reply(409, {
        error: { code: "REVISION_CONFLICT", message: "다른 곳에서 먼저 저장했습니다", details: { currentRevision: 5, draftDsl: theirs } },
      }),
    )

    expect(await saveDraft("wf_1", BODY)).toEqual({ outcome: "conflict", currentRevision: 5, draftDsl: theirs })
  })

  it("treats a 409 without a usable draft as a failure, not a conflict", async () => {
    // The dialog offers a choice between this document and theirs. Without theirs there is no choice
    // to offer, and calling it a conflict would show an empty one.
    vi.stubGlobal("fetch", reply(409, { error: { code: "REVISION_CONFLICT", message: "충돌" } }))

    expect(await saveDraft("wf_1", BODY)).toEqual({ outcome: "failed", message: "충돌" })
  })

  it("treats a 409 whose details are the wrong shape as a failure too", async () => {
    // An absent `details` is one way the contract can be broken; a `details` that is there but holds
    // the wrong types is the other, and it reaches further into the code.
    vi.stubGlobal(
      "fetch",
      reply(409, { error: { code: "REVISION_CONFLICT", message: "충돌", details: { currentRevision: "5", draftDsl: null } } }),
    )

    expect(await saveDraft("wf_1", BODY)).toEqual({ outcome: "failed", message: "충돌" })
  })

  it("passes the engine's own message through on a failure", async () => {
    vi.stubGlobal("fetch", reply(422, { error: { code: "LIMIT_EXCEEDED", message: "워크플로가 너무 큽니다" } }))

    expect(await saveDraft("wf_1", BODY)).toEqual({ outcome: "failed", message: "워크플로가 너무 큽니다" })
  })

  it("names the status when there is no message to pass through", async () => {
    // A proxy error page is not JSON, and `json()` throws on it.
    vi.stubGlobal("fetch", vi.fn((_url: string) => Promise.resolve(new Response("<html>502</html>", { status: 502 }))))

    expect(await saveDraft("wf_1", BODY)).toEqual({ outcome: "failed", message: "저장하지 못했습니다 (502)" })
  })

  it("passes the abort signal through, so a save can be cancelled", async () => {
    const fetchMock = reply(200, { revision: 2 })
    vi.stubGlobal("fetch", fetchMock)
    const controller = new AbortController()
    await saveDraft("wf_1", BODY, { signal: controller.signal })

    expect(fetchMock.mock.calls[0]?.[1]?.signal).toBe(controller.signal)
  })
})
