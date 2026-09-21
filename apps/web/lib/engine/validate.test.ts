import { afterEach, describe, expect, it, vi } from "vitest"

import { emptyDsl } from "@/lib/dsl/document"

import { validateDraft } from "./validate"

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

describe("validateDraft", () => {
  it("POSTs the draft to the proxy", async () => {
    const fetchMock = reply(200, { issues: [] })
    vi.stubGlobal("fetch", fetchMock)
    await validateDraft("wf_1", emptyDsl())

    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(url).toBe("/api/engine/workflows/wf_1/validate")
    expect(init?.method).toBe("POST")
    expect(JSON.parse(String(init?.body))).toEqual({ draftDsl: emptyDsl() })
  })

  it("escapes the workflow id", async () => {
    const fetchMock = reply(200, { issues: [] })
    vi.stubGlobal("fetch", fetchMock)
    await validateDraft("../secrets", emptyDsl())

    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/engine/workflows/..%2Fsecrets/validate")
  })

  it("returns the issues and the node analysis", async () => {
    const nodes = { llm_1: { variables: [], outputSchema: {}, handles: [] } }
    vi.stubGlobal("fetch", reply(200, { issues: [{ severity: "error", code: "X", message: "m" }], nodes }))

    const result = await validateDraft("wf_1", emptyDsl())
    expect(result.issues).toHaveLength(1)
    expect(result.nodes).toEqual(nodes)
  })

  it("reports no analysis when the response carries none", async () => {
    // A structural error stops the walk before the analysis exists; the slice keeps the previous map.
    vi.stubGlobal("fetch", reply(200, { issues: [] }))

    expect((await validateDraft("wf_1", emptyDsl())).nodes).toBeUndefined()
  })

  it("throws on a non-200, which is transport trouble rather than a verdict", async () => {
    // The engine answers 200 even for a draft it cannot parse. A 502 says nothing about the workflow,
    // and treating it as "no issues" would clear every badge on the canvas.
    vi.stubGlobal("fetch", reply(502, { error: { message: "bad gateway" } }))

    await expect(validateDraft("wf_1", emptyDsl())).rejects.toThrow(/502/)
  })

  it("throws rather than guessing when the body is not what the contract says", async () => {
    vi.stubGlobal("fetch", reply(200, { ok: true }))

    await expect(validateDraft("wf_1", emptyDsl())).rejects.toThrow()
  })
})
