import { describe, expect, it } from "vitest"

import { engineTarget } from "./paths"

const NUL = String.fromCharCode(0)

describe("engineTarget", () => {
  it.each([
    [["node-types"], "/node-types"],
    [["healthz"], "/healthz"],
    [["workflows"], "/workflows"],
    [["workflows", "0b6f1e4e-0000-4000-8000-000000000001"], "/workflows/0b6f1e4e-0000-4000-8000-000000000001"],
    [["workflows", "abc", "runs"], "/workflows/abc/runs"],
    [["workflows", "abc", "validate"], "/workflows/abc/validate"],
    [["runs", "abc", "events"], "/runs/abc/events"],
    [["runs", "abc", "nodes"], "/runs/abc/nodes"],
    [["secrets"], "/secrets"],
    [["secrets", "API_TOKEN"], "/secrets/API_TOKEN"],
  ])("passes %j through", (segments, expected) => {
    expect(engineTarget(segments as string[])).toBe(expected)
  })

  it.each([
    [["openapi.json"]],
    [["docs"]],
    [["redoc"]],
    [[]],
    [["metrics"]],
  ])("refuses %j, which is not a route this editor uses", (segments) => {
    expect(engineTarget(segments as string[])).toBeNull()
  })

  it.each([
    [["workflows", ".."]],
    [["workflows", "..", "openapi.json"]],
    [["workflows", "."]],
    [["workflows", "%2e%2e", "openapi.json"]],
    [["workflows", "%2E%2E%2F"]],
    [["workflows", "..%2Fopenapi.json"]],
    [["workflows", "a/b"]],
    [["workflows", "a\\b"]],
  ])("refuses traversal in %j", (segments) => {
    // Next decodes catch-all segments, but a proxy must not depend on how many times something was
    // decoded on the way in: a segment is rejected if it is a traversal raw or once decoded.
    expect(engineTarget(segments as string[])).toBeNull()
  })

  it("refuses a NUL or a control character", () => {
    expect(engineTarget(["workflows", `a${NUL}b`])).toBeNull()
    expect(engineTarget(["workflows", "a\nb"])).toBeNull()
  })

  it("refuses an empty segment, which would collapse the path", () => {
    expect(engineTarget(["workflows", "", "runs"])).toBeNull()
  })

  it("keeps a percent-encoded character that is not a traversal", () => {
    // A workflow id is a UUID, but a secret name or a future segment could legitimately carry one.
    expect(engineTarget(["secrets", "A%20B"])).toBe("/secrets/A%20B")
  })
})
