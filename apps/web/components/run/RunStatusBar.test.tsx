import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { emptyStream, type StreamState } from "@/lib/run/events"

import { RunStatusBar } from "./RunStatusBar"

function show(over: Partial<StreamState> = {}) {
  return render(<RunStatusBar stream={{ ...emptyStream(), ...over }} />)
}

describe("RunStatusBar", () => {
  it("names every run status in Korean", () => {
    for (const [status, label] of [
      ["queued", "대기 중"],
      ["running", "실행 중"],
      ["waiting", "승인 대기"],
      ["succeeded", "성공"],
      ["failed", "실패"],
      ["cancelled", "취소됨"],
    ] as const) {
      const { unmount } = show({ status })
      expect(screen.getByRole("status", { name: "실행 상태" }), status).toHaveTextContent(label)
      unmount()
    }
  })

  it("says the connection dropped while the run is going", () => {
    show({ status: "running", disconnected: true })

    expect(screen.getByRole("status", { name: "실행 상태" })).toHaveTextContent("연결 끊김 — 재연결 중")
  })

  it("says nothing about the connection while it is fine", () => {
    show({ status: "running" })

    expect(screen.queryByText(/재연결/)).toBeNull()
  })

  it("is named apart from the save status bar", () => {
    // Two unnamed live regions on one screen are indistinguishable to a screen reader, and they answer
    // different questions: "is my work safe" and "is my run going".
    show()

    expect(screen.getByRole("status", { name: "실행 상태" })).toHaveAttribute("aria-live", "polite")
  })
})
