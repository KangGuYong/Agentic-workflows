import { fireEvent, renderHook } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { useSaveShortcut } from "./useSaveShortcut"

function ctrlS(target: Window | Element) {
  return fireEvent.keyDown(target, { key: "s", code: "KeyS", ctrlKey: true })
}

describe("useSaveShortcut", () => {
  it("saves on Ctrl+S from anywhere on the page", () => {
    const flush = vi.fn(() => Promise.resolve())
    renderHook(() => useSaveShortcut(flush))
    const input = document.createElement("input")
    document.body.append(input)

    ctrlS(input)

    expect(flush).toHaveBeenCalledTimes(1)
    input.remove()
  })

  it("takes the keypress, so the browser does not offer to save the page as a file", () => {
    const flush = vi.fn(() => Promise.resolve())
    renderHook(() => useSaveShortcut(flush))

    // `fireEvent` returns false when a listener called `preventDefault`.
    expect(ctrlS(window)).toBe(false)
  })

  it("ignores other keys, and leaves them to the browser", () => {
    const flush = vi.fn(() => Promise.resolve())
    renderHook(() => useSaveShortcut(flush))

    fireEvent.keyDown(window, { key: "s", code: "KeyS" })
    // Ctrl+C is the copy every text field on this screen needs.
    expect(fireEvent.keyDown(window, { key: "c", code: "KeyC", ctrlKey: true })).toBe(true)

    expect(flush).not.toHaveBeenCalled()
  })

  it("stops listening when the editor unmounts", () => {
    const flush = vi.fn(() => Promise.resolve())
    const { unmount } = renderHook(() => useSaveShortcut(flush))
    unmount()

    ctrlS(window)

    expect(flush).not.toHaveBeenCalled()
  })
})
