import { describe, expect, it } from "vitest"

import { localTime, relativeTime } from "./time"

describe("localTime", () => {
  it("formats a timestamp", () => {
    expect(localTime("2026-09-21T04:00:00.000Z")).not.toBe("")
  })

  it("is empty for nothing, rather than showing Invalid Date", () => {
    // A bad timestamp should cost that one cell, not the readability of the whole row.
    expect(localTime(undefined)).toBe("")
    expect(localTime("")).toBe("")
    expect(localTime("어제")).toBe("")
  })
})

describe("relativeTime", () => {
  const AT = Date.parse("2026-09-21T04:00:00.000Z")

  it("counts up through the units", () => {
    expect(relativeTime("2026-09-21T04:00:00.000Z", AT + 30_000)).toBe("방금")
    expect(relativeTime("2026-09-21T04:00:00.000Z", AT + 5 * 60_000)).toBe("5분 전")
    expect(relativeTime("2026-09-21T04:00:00.000Z", AT + 3 * 3_600_000)).toBe("3시간 전")
    expect(relativeTime("2026-09-21T04:00:00.000Z", AT + 2 * 86_400_000)).toBe("2일 전")
  })

  it("switches units at the boundary", () => {
    expect(relativeTime("2026-09-21T04:00:00.000Z", AT + 59_000)).toBe("방금")
    expect(relativeTime("2026-09-21T04:00:00.000Z", AT + 60_000)).toBe("1분 전")
    expect(relativeTime("2026-09-21T04:00:00.000Z", AT + 3_600_000)).toBe("1시간 전")
    expect(relativeTime("2026-09-21T04:00:00.000Z", AT + 86_400_000)).toBe("1일 전")
  })

  it("does not go backwards when the clocks disagree", () => {
    expect(relativeTime("2026-09-21T04:00:00.000Z", AT - 60_000)).toBe("방금")
  })

  it("is empty for nothing", () => {
    expect(relativeTime(undefined, AT)).toBe("")
    expect(relativeTime("어제", AT)).toBe("")
  })
})
