import { describe, expect, it } from "vitest"

import { forcedSingleAttempt } from "./retry"

describe("forcedSingleAttempt", () => {
  it("is true for the http methods the engine refuses to retry", () => {
    // `engine/nodes/http_request.py::policy_for`: POST and PATCH are not idempotent, so a retry can
    // double a payment. The engine overrides whatever the DSL says, so the panel must not offer a
    // number it will silently ignore.
    expect(forcedSingleAttempt("http_request", { method: "POST" })).toBe(true)
    expect(forcedSingleAttempt("http_request", { method: "PATCH" })).toBe(true)
  })

  it("is false for the idempotent methods", () => {
    for (const method of ["GET", "PUT", "DELETE", "HEAD"]) {
      expect(forcedSingleAttempt("http_request", { method })).toBe(false)
    }
  })

  it("treats a missing method as the engine's default, GET", () => {
    expect(forcedSingleAttempt("http_request", {})).toBe(false)
  })

  it("is false for every other node type", () => {
    expect(forcedSingleAttempt("llm", { method: "POST" })).toBe(false)
  })
})
