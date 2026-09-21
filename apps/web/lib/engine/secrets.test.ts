import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { deleteSecret, listSecrets, putSecret } from "./secrets"

let calls: { url: string; init?: RequestInit }[]

function answer(...responses: Response[]) {
  const queue = [...responses]
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init })
      return Promise.resolve(queue.shift() ?? new Response(null, { status: 500 }))
    }),
  )
}

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })
}

beforeEach(() => {
  calls = []
})

afterEach(() => {
  vi.unstubAllGlobals()
})

const ROW = { name: "API_KEY", createdAt: "2026-09-01T00:00:00.000Z", updatedAt: "2026-09-20T00:00:00.000Z" }

describe("listSecrets", () => {
  it("lists names and times", async () => {
    answer(json(200, { secrets: [ROW] }))

    expect(await listSecrets()).toEqual({ outcome: "ok", secrets: [ROW] })
    expect(calls[0]?.url).toBe("/api/engine/secrets")
  })

  it("never carries a value, because the API has none to give", async () => {
    // There is no endpoint that reads a secret back. A list row is a name and two timestamps.
    answer(json(200, { secrets: [ROW] }))
    const result = await listSecrets()

    expect(result.outcome === "ok" && Object.keys(result.secrets[0] ?? {})).toEqual([
      "name",
      "createdAt",
      "updatedAt",
    ])
  })

  it("reports a failure rather than an empty list", async () => {
    answer(json(503, { error: { message: "시크릿 저장소가 설정되지 않았습니다" } }))

    expect(await listSecrets()).toEqual({
      outcome: "failed",
      message: "시크릿 저장소가 설정되지 않았습니다",
    })
  })
})

describe("putSecret", () => {
  it("PUTs the value under the name", async () => {
    answer(new Response(null, { status: 204 }))

    expect(await putSecret("API_KEY", "sk-live-abcdefgh")).toEqual({ outcome: "ok" })
    expect(calls[0]?.url).toBe("/api/engine/secrets/API_KEY")
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({ value: "sk-live-abcdefgh" })
  })

  it("passes the engine's bounds message through without the value", async () => {
    answer(json(422, { error: { message: "시크릿 값은 8~4096자여야 합니다" } }))

    const result = await putSecret("API_KEY", "short")
    expect(result).toEqual({ outcome: "failed", message: "시크릿 값은 8~4096자여야 합니다" })
    expect(JSON.stringify(result)).not.toContain("short")
  })

  it("escapes the name", async () => {
    answer(new Response(null, { status: 204 }))
    await putSecret("../workflows", "sk-live-abcdefgh")

    expect(calls[0]?.url).toBe("/api/engine/secrets/..%2Fworkflows")
  })
})

describe("deleteSecret", () => {
  it("reports the engine's 204", async () => {
    answer(new Response(null, { status: 204 }))

    expect(await deleteSecret("API_KEY")).toEqual({ outcome: "ok" })
  })

  it("reports a name that is not there", async () => {
    answer(json(404, { error: { message: "시크릿을 찾을 수 없습니다" } }))

    expect(await deleteSecret("GONE")).toEqual({ outcome: "failed", message: "시크릿을 찾을 수 없습니다" })
  })
})
