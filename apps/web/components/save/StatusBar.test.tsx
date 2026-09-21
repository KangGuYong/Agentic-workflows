import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { emptyDsl } from "@/lib/dsl/document"
import { createSaveStore, type SaveState } from "@/store/save"

import { ConflictDialog } from "./ConflictDialog"
import { savedAtText, StatusBar } from "./StatusBar"

function state(over: Partial<SaveState> = {}): SaveState {
  const store = createSaveStore({
    revision: 1,
    getDsl: emptyDsl,
    save: () => Promise.resolve({ outcome: "saved", revision: 2 }),
    onReload: () => {},
  })
  return { ...store.getState(), ...over }
}

describe("savedAtText", () => {
  const AT = 1_700_000_000_000

  it("is nothing before the first successful save", () => {
    expect(savedAtText(null, AT)).toBeNull()
  })

  it("reads 방금 under a minute", () => {
    expect(savedAtText(AT, AT + 59_000)).toBe("방금")
  })

  it("counts minutes, then hours", () => {
    expect(savedAtText(AT, AT + 60_000)).toBe("1분 전")
    expect(savedAtText(AT, AT + 59 * 60_000)).toBe("59분 전")
    expect(savedAtText(AT, AT + 60 * 60_000)).toBe("1시간 전")
  })

  it("does not go backwards when the clocks disagree", () => {
    // The timestamp is this browser's, but nothing guarantees the two readings are ordered.
    expect(savedAtText(AT, AT - 5_000)).toBe("방금")
  })
})

describe("StatusBar", () => {
  it("names each state", () => {
    for (const [status, label] of [
      ["saved", "저장됨"],
      ["pending", "저장 대기 중"],
      ["saving", "저장 중"],
      ["conflict", "다른 곳에서 변경됨"],
      ["error", "저장 실패"],
    ] as const) {
      const { unmount } = render(<StatusBar state={state({ status })} now={0} />)
      expect(screen.getByRole("status", { name: "저장 상태" })).toHaveTextContent(label)
      unmount()
    }
  })

  it("shows the engine's own words on a failure, not a generic line", () => {
    render(<StatusBar state={state({ status: "error", error: "워크플로가 너무 큽니다" })} now={0} />)

    expect(screen.getByRole("status", { name: "저장 상태" })).toHaveTextContent("워크플로가 너무 큽니다")
  })

  it("shows when the last save landed", () => {
    render(<StatusBar state={state({ savedAt: 1_000 })} now={61_000} />)

    expect(screen.getByRole("status", { name: "저장 상태" })).toHaveTextContent("마지막 저장 1분 전")
  })

  it("announces itself, and says which status it is", () => {
    // The canvas shows its own `role="status"` for edit errors. Two unnamed live regions on one screen
    // are indistinguishable to a screen reader.
    render(<StatusBar state={state()} now={0} />)

    const bar = screen.getByRole("status", { name: "저장 상태" })
    expect(bar).toHaveAttribute("aria-live", "polite")
  })
})

describe("ConflictDialog", () => {
  const CONFLICT = { currentRevision: 5, draftDsl: emptyDsl() }

  it("stays closed while there is no conflict", () => {
    render(<ConflictDialog state={state()} />)

    expect(screen.queryByRole("dialog")).toBeNull()
  })

  it("opens on a conflict and offers both choices with what each costs", () => {
    render(<ConflictDialog state={state({ status: "conflict", conflict: CONFLICT })} />)

    expect(screen.getByRole("dialog")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /저장된 내용 불러오기/ })).toHaveTextContent(
      "이 화면에서 한 편집은 사라집니다",
    )
    expect(screen.getByRole("button", { name: /내 변경으로 덮어쓰기/ })).toHaveTextContent(
      "다른 곳에서 저장한 내용이 사라집니다",
    )
  })

  it("has no default action, because both choices lose somebody's work", () => {
    render(<ConflictDialog state={state({ status: "conflict", conflict: CONFLICT })} />)

    for (const button of screen.getAllByRole("button")) {
      expect(button).toHaveAttribute("type", "button")
    }
  })
})
