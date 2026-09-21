import { describe, expect, it } from "vitest"

import { GET, POST } from "./route"

describe("GET /api/health", () => {
  it("answers ok without touching the engine", async () => {
    // A healthcheck that called the engine would report this container unhealthy whenever the engine
    // was down, and Docker would restart a process that is working perfectly.
    const response = GET()

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual({ status: "ok" })
  })

  it("is not cacheable", async () => {
    // A cached liveness answer keeps saying "ok" after the process stops being able to produce one.
    expect(GET().headers.get("cache-control")).toBe("no-store")
  })
})

describe("POST /api/health", () => {
  it("echoes the body, which is what makes it a usable http_request target", async () => {
    const response = await POST(
      new Request("http://localhost/api/health", {
        method: "POST",
        body: JSON.stringify({ name: "세계" }),
      }),
    )

    expect(await response.json()).toEqual({ status: "ok", echo: { name: "세계" } })
  })

  it("answers a body that is not JSON rather than failing", async () => {
    // The engine can send whatever a tenant's template produced; a 500 here would look like the
    // workflow's fault.
    const response = await POST(new Request("http://localhost/api/health", { method: "POST", body: "무엇" }))

    expect(response.status).toBe(200)
    expect(await response.json()).toEqual({ status: "ok", echo: null })
  })
})
