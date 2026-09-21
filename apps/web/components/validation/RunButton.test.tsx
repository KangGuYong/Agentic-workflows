import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import type { Issue } from "@/store/validation"

import { RunButton, runBlockedReason } from "./RunButton"

function issue(over: Partial<Issue> = {}): Issue {
  return { severity: "error", code: "INVALID_CONFIG", message: "설정 오류", ...over }
}

describe("runBlockedReason", () => {
  it("is nothing when the workflow has nodes and no errors", () => {
    expect(runBlockedReason([], 3)).toBeNull()
    expect(runBlockedReason([issue({ severity: "warning" })], 3)).toBeNull()
  })

  it("counts the errors", () => {
    expect(runBlockedReason([issue(), issue()], 3)).toBe("오류 2건을 먼저 해결해 주세요")
  })

  it("counts only errors, not warnings", () => {
    // A warning is the engine saying "this will run, and here is what to watch". Blocking on it would
    // make the warning indistinguishable from an error.
    expect(runBlockedReason([issue(), issue({ severity: "warning" })], 3)).toBe("오류 1건을 먼저 해결해 주세요")
  })

  it("asks for a node before it asks about errors", () => {
    // An empty canvas has no errors *because* it has nothing; "오류 0건" would be a confusing thing to
    // read next to a disabled button.
    expect(runBlockedReason([], 0)).toBe("노드를 먼저 놓아 주세요")
  })
})

describe("RunButton", () => {
  it("is disabled while an error stands, and enabled once it is fixed", () => {
    const { rerender } = render(<RunButton issues={[issue()]} nodeCount={2} />)
    const button = screen.getByRole("button", { name: /실행/ })
    expect(button).toBeDisabled()

    rerender(<RunButton issues={[]} nodeCount={2} />)
    expect(screen.getByRole("button", { name: /실행/ })).toBeEnabled()
  })

  it("says why, where a screen reader will read it", () => {
    // A `title` tooltip does not exist on a touch screen and is not announced by every screen reader.
    render(<RunButton issues={[issue(), issue()]} nodeCount={2} />)

    expect(screen.getByRole("button", { name: /실행/ })).toHaveAccessibleDescription(
      "오류 2건을 먼저 해결해 주세요",
    )
  })

  it("carries no stale reason once it is enabled", () => {
    render(<RunButton issues={[]} nodeCount={2} />)

    const button = screen.getByRole("button", { name: /실행/ })
    expect(button).not.toHaveAttribute("title")
    expect(button).toHaveAccessibleDescription("")
  })
})
