import { afterEach, describe, expect, it, vi } from "vitest"

import { engineEnv } from "./env"

afterEach(() => {
  vi.unstubAllEnvs()
})

function set(url: string | undefined, token: string | undefined) {
  vi.stubEnv("ENGINE_API_URL", url)
  vi.stubEnv("ENGINE_API_TOKEN", token)
}

describe("engineEnv", () => {
  it("returns the configured base URL and token", () => {
    set("http://api:8000", "dev-token-0123456789")
    expect(engineEnv()).toEqual({ baseUrl: new URL("http://api:8000"), token: "dev-token-0123456789" })
  })

  it("names the variable that is missing", () => {
    // A misconfigured proxy otherwise shows up as 401s from the engine, which look like the operator's
    // token being wrong rather than the web container never having been given one.
    set(undefined, "dev-token-0123456789")
    expect(() => engineEnv()).toThrow(/ENGINE_API_URL/)
    set("http://api:8000", undefined)
    expect(() => engineEnv()).toThrow(/ENGINE_API_TOKEN/)
    set("", "")
    expect(() => engineEnv()).toThrow(/ENGINE_API_URL/)
  })

  it("refuses a base URL that is not a URL", () => {
    set("api:8000", "dev-token-0123456789")
    expect(() => engineEnv()).toThrow(/ENGINE_API_URL/)
  })

  it("refuses a base URL carrying a path, query or fragment", () => {
    // `new URL("/workflows", base)` discards a base path, so a configured prefix would be silently
    // dropped and every request would go somewhere the operator did not intend.
    set("http://api:8000/engine", "dev-token-0123456789")
    expect(() => engineEnv()).toThrow(/경로/)
    set("http://api:8000/?a=1", "dev-token-0123456789")
    expect(() => engineEnv()).toThrow(/경로/)
  })

  it("accepts a bare origin with or without a trailing slash", () => {
    set("http://api:8000/", "t".repeat(16))
    expect(engineEnv().baseUrl.origin).toBe("http://api:8000")
    set("https://engine.example.com", "t".repeat(16))
    expect(engineEnv().baseUrl.origin).toBe("https://engine.example.com")
  })

  it("refuses a token shorter than the engine's own minimum", () => {
    // engine/config.py::MIN_API_TOKEN_LEN is 16. A shorter one cannot be the engine's token, so the
    // proxy would send something guaranteed to 401 on every request.
    set("http://api:8000", "short")
    expect(() => engineEnv()).toThrow(/ENGINE_API_TOKEN/)
  })
})
