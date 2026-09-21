import { describe, expect, it } from "vitest"

import {
  durationMs,
  durationText,
  hasDetail,
  isRedacted,
  REDACTED_TEXT,
  runsForNode,
  scrubRedactions,
  type NodeRun,
} from "./trace"

function run(over: Partial<NodeRun> = {}): NodeRun {
  return {
    nodeId: "llm_1",
    execIndex: 1,
    attempt: 1,
    status: "succeeded",
    input: null,
    output: null,
    error: null,
    meta: null,
    truncated: false,
    tokensIn: 0,
    tokensOut: 0,
    startedAt: "2026-09-21T04:00:00.000Z",
    finishedAt: "2026-09-21T04:00:01.500Z",
    ...over,
  }
}

describe("runsForNode", () => {
  it("keeps only that node's attempts", () => {
    const rows = [run(), run({ nodeId: "http_1" }), run({ attempt: 2 })]

    expect(runsForNode(rows, "llm_1")).toHaveLength(2)
  })

  it("puts the most recent attempt first", () => {
    // That is the one someone opening the panel is looking for.
    const rows = [run({ attempt: 1 }), run({ attempt: 3 }), run({ attempt: 2 })]

    expect(runsForNode(rows, "llm_1").map((r) => r.attempt)).toEqual([3, 2, 1])
  })

  it("orders by iteration before attempt", () => {
    // A node in a loop has one row per iteration, each with its own attempts.
    const rows = [
      run({ execIndex: 1, attempt: 2 }),
      run({ execIndex: 2, attempt: 1 }),
      run({ execIndex: 1, attempt: 1 }),
    ]

    expect(runsForNode(rows, "llm_1").map((r) => [r.execIndex, r.attempt])).toEqual([[2, 1], [1, 2], [1, 1]])
  })

  it("is empty for a node that has not run", () => {
    expect(runsForNode([run()], "nobody")).toEqual([])
  })
})

describe("durationMs", () => {
  it("is the span between the two timestamps", () => {
    expect(durationMs(run())).toBe(1500)
  })

  it("is null while the attempt is still going", () => {
    expect(durationMs(run({ finishedAt: null }))).toBeNull()
  })

  it("is null when a timestamp cannot be read", () => {
    expect(durationMs(run({ startedAt: "어제" }))).toBeNull()
  })

  it("never goes negative", () => {
    // The two stamps come from the same clock, but nothing guarantees monotonicity across a process
    // restart, and "-3ms" is not a thing to show anyone.
    expect(durationMs(run({ finishedAt: "2026-09-21T03:59:59.000Z" }))).toBe(0)
  })
})

describe("durationText", () => {
  it("says 진행 중 while there is no end", () => {
    expect(durationText(null)).toBe("진행 중")
  })

  it("uses milliseconds, seconds and minutes as the size warrants", () => {
    expect(durationText(320)).toBe("320ms")
    expect(durationText(1500)).toBe("1.5초")
    expect(durationText(65_000)).toBe("1분 5초")
    expect(durationText(120_000)).toBe("2분")
  })

  it("switches units at the boundary rather than showing 1000ms", () => {
    expect(durationText(999)).toBe("999ms")
    expect(durationText(1000)).toBe("1.0초")
    expect(durationText(59_999)).toBe("60.0초")
    expect(durationText(60_000)).toBe("1분")
  })
})

describe("isRedacted", () => {
  it("recognises the engine's marker", () => {
    expect(isRedacted("[REDACTED]")).toBe(true)
  })

  it("does not treat anything else as one", () => {
    for (const value of ["REDACTED", "[redacted]", "[REDACTED] 뒤에 더", null, 0, {}]) {
      expect(isRedacted(value), JSON.stringify(value)).toBe(false)
    }
  })
})

describe("hasDetail", () => {
  it("is false for an attempt with nothing recorded", () => {
    expect(hasDetail(run())).toBe(false)
  })

  it("is true when there is an input, an output or an error", () => {
    expect(hasDetail(run({ input: {} }))).toBe(true)
    expect(hasDetail(run({ output: { text: "x" } }))).toBe(true)
    expect(hasDetail(run({ error: { code: "TIMEOUT" } }))).toBe(true)
  })
})

describe("scrubRedactions", () => {
  it("replaces a marker with something that cannot be mistaken for the secret", () => {
    // Needed wherever a value is printed rather than walked: `JSON.stringify` would otherwise put the
    // marker straight back on screen.
    expect(scrubRedactions("[REDACTED]")).toBe(REDACTED_TEXT)
  })

  it("reaches markers inside objects and arrays, however deep", () => {
    const scrubbed = scrubRedactions({ a: { b: [{ c: "[REDACTED]" }] }, d: ["[REDACTED]"] })

    expect(JSON.stringify(scrubbed)).not.toContain("[REDACTED]")
  })

  it("leaves everything else exactly as it was", () => {
    const value = { a: "문자열", b: 1, c: true, d: null, e: [1, 2], f: {} }

    expect(scrubRedactions(value)).toEqual(value)
  })

  it("does not put the marker back through its own replacement", () => {
    expect(String(REDACTED_TEXT)).not.toContain("[REDACTED]")
  })
})
