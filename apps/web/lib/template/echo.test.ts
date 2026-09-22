import { describe, expect, it } from "vitest"

import { MAX_PENDING, echoes } from "./echo"

describe("echoes", () => {
  it("recognises the parent handing back exactly what was reported", () => {
    const tracker = echoes()
    tracker.emitted("a")

    expect(tracker.own("a")).toBe(true)
  })

  it("treats a value that was never reported as the parent's own change", () => {
    const tracker = echoes()
    tracker.emitted("a")

    expect(tracker.own("b")).toBe(false)
  })

  it("recognises a stale echo from a parent that commits late", () => {
    // The IME race: three composition updates reported, the parent's render of the second one lands
    // after the third. Writing "아" back here is what destroyed the committed syllable.
    const tracker = echoes()
    tracker.emitted("ㅇ")
    tracker.emitted("아")
    tracker.emitted("안")

    expect(tracker.own("아")).toBe(true)
    expect(tracker.own("안")).toBe(true)
  })

  it("consumes every report up to the one that came back", () => {
    // A parent that batched the first two reports into one render never echoes "a"; when "b" comes
    // back, "a" is caught up too and must not linger to be mistaken for an echo later.
    const tracker = echoes()
    tracker.emitted("a")
    tracker.emitted("b")

    expect(tracker.own("b")).toBe(true)
    expect(tracker.own("a")).toBe(false)
  })

  it("does not recognise the same echo twice", () => {
    const tracker = echoes()
    tracker.emitted("a")
    tracker.own("a")

    expect(tracker.own("a")).toBe(false)
  })

  it("forgets earlier reports once the parent overrides them", () => {
    // Undo in the parent restores "a" after the editor reported "a" then "b". The parent's "z" in
    // between made everything before it moot: this "a" is the parent's, not an echo.
    const tracker = echoes()
    tracker.emitted("a")
    tracker.emitted("b")
    tracker.own("z")

    expect(tracker.own("a")).toBe(false)
  })

  it("bounds what a parent that never echoes can make it remember", () => {
    const tracker = echoes()
    for (let i = 0; i <= MAX_PENDING; i += 1) tracker.emitted(`doc ${i}`)

    // The oldest report was dropped; the newest is still known.
    expect(tracker.own("doc 0")).toBe(false)
    tracker.emitted("again")
    expect(tracker.own("again")).toBe(true)
  })
})
