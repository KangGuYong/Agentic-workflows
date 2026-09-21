import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { DELETE, GET, POST, PUT } from "./route"

const TOKEN = "engine-token-0123456789"

interface Sent {
  url: string
  method: string
  headers: Headers
  init: RequestInit
}

let sent: Sent[]

/** The upstream engine, replaced by a spy that records exactly what the proxy sent it. */
function upstream(response: Response) {
  const fetch = vi.fn(async (input: string | URL | Request, init: RequestInit = {}) => {
    const url = input instanceof Request ? input.url : String(input)
    sent.push({ url, method: init.method ?? "GET", headers: new Headers(init.headers), init })
    return response
  })
  vi.stubGlobal("fetch", fetch)
  return fetch
}

function context(...path: string[]) {
  return { params: Promise.resolve({ path }) }
}

beforeEach(() => {
  sent = []
  vi.stubEnv("ENGINE_API_URL", "http://api:8000")
  vi.stubEnv("ENGINE_API_TOKEN", TOKEN)
})

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe("the engine proxy", () => {
  it("forwards an allowed path to the engine", async () => {
    upstream(Response.json({ workflows: [] }))
    const response = await GET(new Request("http://web/api/engine/workflows"), context("workflows"))

    expect(response.status).toBe(200)
    expect(sent[0]?.url).toBe("http://api:8000/workflows")
    await expect(response.json()).resolves.toEqual({ workflows: [] })
  })

  it("answers 404 for a path outside the allowlist, without calling the engine", async () => {
    const fetch = upstream(Response.json({ openapi: "3.1.0" }))
    const response = await GET(new Request("http://web/api/engine/openapi.json"), context("openapi.json"))

    // 404, not 403: a proxy that distinguishes "exists but forbidden" from "no such path" tells a caller
    // which engine routes are real.
    expect(response.status).toBe(404)
    expect(fetch).not.toHaveBeenCalled()
  })

  it("adds the engine token and drops one the caller supplied", async () => {
    upstream(Response.json({}))
    await GET(
      new Request("http://web/api/engine/workflows", { headers: { authorization: "Bearer ATTACKER" } }),
      context("workflows"),
    )

    expect(sent[0]?.headers.get("authorization")).toBe(`Bearer ${TOKEN}`)
  })

  it("forwards Last-Event-ID so a reconnecting stream resumes", async () => {
    upstream(new Response("", { headers: { "content-type": "text/event-stream" } }))
    await GET(
      new Request("http://web/api/engine/runs/r1/events", { headers: { "last-event-id": "42" } }),
      context("runs", "r1", "events"),
    )

    // EventSource sends this by itself on reconnect; it is not in any default forward set, so the proxy
    // has to copy it deliberately or the engine replays the run from seq 0 every time.
    expect(sent[0]?.headers.get("last-event-id")).toBe("42")
  })

  it("forwards Idempotency-Key", async () => {
    upstream(Response.json({ runId: "r1" }, { status: 202 }))
    await POST(
      new Request("http://web/api/engine/workflows/w1/runs", {
        method: "POST",
        headers: { "content-type": "application/json", "idempotency-key": "abc-123" },
        body: JSON.stringify({ inputs: {}, revision: 2 }),
      }),
      context("workflows", "w1", "runs"),
    )

    expect(sent[0]?.headers.get("idempotency-key")).toBe("abc-123")
  })

  it("does not forward hop-by-hop headers or cookies", async () => {
    upstream(Response.json({}))
    await GET(
      new Request("http://web/api/engine/workflows", {
        headers: { connection: "keep-alive", cookie: "session=abc", "x-forwarded-for": "10.0.0.1" },
      }),
      context("workflows"),
    )

    const headers = sent[0]?.headers
    expect(headers?.get("connection")).toBeNull()
    expect(headers?.get("cookie")).toBeNull()
    expect(headers?.get("x-forwarded-for")).toBeNull()
    // `host` is set by fetch from the target URL; forwarding the browser's would point the engine's own
    // Host header at the web container.
    expect(headers?.get("host")).toBeNull()
  })

  it("passes an error envelope through unchanged, with its status", async () => {
    const envelope = {
      error: {
        code: "REVISION_CONFLICT",
        message: "다른 곳에서 먼저 저장했습니다",
        details: { currentRevision: 12, draftDsl: { version: "1" } },
      },
    }
    upstream(Response.json(envelope, { status: 409 }))
    const response = await PUT(
      new Request("http://web/api/engine/workflows/w1", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ draftDsl: {}, revision: 11 }),
      }),
      context("workflows", "w1"),
    )

    // The editor's conflict dialog reads details.currentRevision; a proxy that reshapes or swallows the
    // envelope breaks it in a way no status code reveals.
    expect(response.status).toBe(409)
    await expect(response.json()).resolves.toEqual(envelope)
  })

  it("hands the response body through without reading it", async () => {
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("event: run_queued\ndata: {}\n\n"))
        controller.close()
      },
    })
    upstream(new Response(body, { headers: { "content-type": "text/event-stream" } }))
    const response = await GET(
      new Request("http://web/api/engine/runs/r1/events"),
      context("runs", "r1", "events"),
    )

    expect(response.headers.get("content-type")).toBe("text/event-stream")
    await expect(response.text()).resolves.toContain("run_queued")
  })

  it("passes the caller's abort signal upstream", async () => {
    // Task 0 measured this: without the signal, closing a tab leaves the engine's SSE connection -- and
    // the Redis pubsub connection behind it -- open until the run itself ends.
    upstream(Response.json({}))
    const controller = new AbortController()
    await GET(
      new Request("http://web/api/engine/runs/r1/events", { signal: controller.signal }),
      context("runs", "r1", "events"),
    )

    expect(sent[0]?.init.signal).toBeDefined()
    expect(sent[0]?.init.signal?.aborted).toBe(false)
    controller.abort()
    expect(sent[0]?.init.signal?.aborted).toBe(true)
  })

  it("keeps the query string", async () => {
    upstream(Response.json({ runs: [] }))
    await GET(
      new Request("http://web/api/engine/workflows/w1/runs?cursor=abc&limit=20"),
      context("workflows", "w1", "runs"),
    )

    expect(sent[0]?.url).toBe("http://api:8000/workflows/w1/runs?cursor=abc&limit=20")
  })

  it("does not follow a redirect the engine returns", async () => {
    upstream(Response.json({}))
    await GET(new Request("http://web/api/engine/workflows"), context("workflows"))

    // Following one would let a redirect move the request off the allowlisted path.
    expect(sent[0]?.init.redirect).toBe("manual")
  })

  it("carries the method through", async () => {
    upstream(new Response(null, { status: 204 }))
    const response = await DELETE(
      new Request("http://web/api/engine/secrets/API_TOKEN", { method: "DELETE" }),
      context("secrets", "API_TOKEN"),
    )

    expect(sent[0]?.method).toBe("DELETE")
    expect(response.status).toBe(204)
  })
})
