import { fireEvent, renderHook } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { useSaveShortcut } from "./useSaveShortcut"

function ctrlC(target: Window | Element) {
  return fireEvent.keyDown(target, { key: "c", code: "KeyC", ctrlKey: true })
}

describe("useSaveShortcut", () => {
  it("saves on Ctrl+C from anywhere on the page", () => {
    const flush = vi.fn(() => Promise.resolve())
    renderHook(() => useSaveShortcut(flush))
    const input = document.createElement("input")
    document.body.append(input)

    ctrlC(input)

    expect(flush).toHaveBeenCalledTimes(1)
    input.remove()
  })

  it("leaves the keypress to the browser, so it still copies", () => {
    const flush = vi.fn(() => Promise.resolve())
    renderHook(() => useSaveShortcut(flush))

    // `fireEvent` returns false when a listener called `preventDefault`.
    expect(ctrlC(window)).toBe(true)
  })

  it("ignores other keys", () => {
    const flush = vi.fn(() => Promise.resolve())
    renderHook(() => useSaveShortcut(flush))

    fireEvent.keyDown(window, { key: "c", code: "KeyC" })
    fireEvent.keyDown(window, { key: "s", code: "KeyS", ctrlKey: true })

    expect(flush).not.toHaveBeenCalled()
  })

  it("stops listening when the editor unmounts", () => {
    const flush = vi.fn(() => Promise.resolve())
    const { unmount } = renderHook(() => useSaveShortcut(flush))
    unmount()

    ctrlC(window)

    expect(flush).not.toHaveBeenCalled()
  })
})
