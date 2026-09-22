import { describe, expect, it } from "vitest"

import { isSaveShortcut } from "./shortcut"

function press(over: Partial<Parameters<typeof isSaveShortcut>[0]> = {}) {
  return isSaveShortcut({ key: "c", code: "KeyC", ctrlKey: true, metaKey: false, altKey: false, repeat: false, ...over })
}

describe("isSaveShortcut", () => {
  it("is Ctrl+C", () => {
    expect(press()).toBe(true)
    expect(press({ key: "C" })).toBe(true)
  })

  it("is ⌘C as well, which is the same chord on a Mac keyboard", () => {
    expect(press({ ctrlKey: false, metaKey: true })).toBe(true)
  })

  it("recognises the key under a Korean IME, which reports it as ㅊ", () => {
    expect(press({ key: "ㅊ" })).toBe(true)
  })

  it("is not a plain c, and not Ctrl with another key", () => {
    expect(press({ ctrlKey: false })).toBe(false)
    expect(press({ key: "v", code: "KeyV" })).toBe(false)
  })

  it("is not Ctrl+Alt+C, which is a character on some layouts", () => {
    expect(press({ altKey: true })).toBe(false)
  })

  it("fires once per press, not on every auto-repeat", () => {
    expect(press({ repeat: true })).toBe(false)
  })
})
