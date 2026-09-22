import { describe, expect, it } from "vitest"

import { isSaveShortcut } from "./shortcut"

function press(over: Partial<Parameters<typeof isSaveShortcut>[0]> = {}) {
  return isSaveShortcut({ key: "s", code: "KeyS", ctrlKey: true, metaKey: false, altKey: false, repeat: false, ...over })
}

describe("isSaveShortcut", () => {
  it("is Ctrl+S", () => {
    expect(press()).toBe(true)
    expect(press({ key: "S" })).toBe(true)
  })

  it("is ⌘S as well, which is the same chord on a Mac keyboard", () => {
    expect(press({ ctrlKey: false, metaKey: true })).toBe(true)
  })

  it("recognises the key under a Korean IME, which reports it as ㄴ", () => {
    expect(press({ key: "ㄴ" })).toBe(true)
  })

  it("follows the character on a layout that moves it, as the browser's own Ctrl+S does", () => {
    // Dvorak puts S on the physical D key. Matching only the physical key would put save somewhere
    // else than where every other application on that keyboard has it.
    expect(press({ key: "s", code: "KeyO" })).toBe(true)
  })

  it("is not a plain s, and not Ctrl with another key", () => {
    expect(press({ ctrlKey: false })).toBe(false)
    expect(press({ key: "c", code: "KeyC" })).toBe(false)
  })

  it("is not Ctrl+Alt+S, which is a character on some layouts", () => {
    expect(press({ altKey: true })).toBe(false)
  })

  it("fires once per press, not on every auto-repeat", () => {
    expect(press({ repeat: true })).toBe(false)
  })
})
