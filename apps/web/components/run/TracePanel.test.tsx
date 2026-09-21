import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import type { NodeRun } from "@/lib/run/trace"

import { TracePanel } from "./TracePanel"

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

function show(over: Partial<Parameters<typeof TracePanel>[0]> = {}) {
  const props = { nodeId: "llm_1", runs: [run()], hasMore: false, ...over }
  return { ...render(<TracePanel {...props} />), props }
}

describe("when there is nothing to show", () => {
  it("says the node has not run, rather than showing an empty panel", () => {
    // "Nothing here" and "this has not run" look the same and mean different things.
    show({ runs: [] })

    expect(screen.getByText("아직 실행되지 않았습니다.")).toBeInTheDocument()
  })

  it("says so for a node whose siblings did run", () => {
    show({ runs: [run({ nodeId: "http_1" })] })

    expect(screen.getByText("아직 실행되지 않았습니다.")).toBeInTheDocument()
  })

  it("reports a failure to load instead of pretending there is no history", () => {
    show({ runs: [], error: "실행 기록을 불러오지 못했습니다 (502)" })

    expect(screen.getByText(/502/)).toBeInTheDocument()
    expect(screen.queryByText("아직 실행되지 않았습니다.")).toBeNull()
  })
})

describe("the attempt rows", () => {
  it("shows one per attempt, newest first", () => {
    show({ runs: [run({ attempt: 1 }), run({ attempt: 2 })] })

    const rows = screen.getAllByRole("button")
    expect(rows[0]).toHaveTextContent("2번째 시도")
    expect(rows[1]).toHaveTextContent("1번째 시도")
  })

  it("shows the iteration only when there is more than one", () => {
    const { unmount } = show({ runs: [run()] })
    expect(screen.queryByText(/회차/)).toBeNull()
    unmount()

    show({ runs: [run({ execIndex: 1 }), run({ execIndex: 2 })] })
    expect(screen.getAllByText(/회차/).length).toBeGreaterThan(0)
  })

  it("shows the status with a glyph and a Korean name", () => {
    show({ runs: [run({ status: "failed" })] })

    expect(screen.getByRole("img", { name: "상태: 실패" })).toBeInTheDocument()
  })

  it("shows how long it took", () => {
    show()

    expect(screen.getByText("1.5초")).toBeInTheDocument()
  })

  it("says 진행 중 for an attempt still running", () => {
    show({ runs: [run({ status: "running", finishedAt: null })] })

    expect(screen.getByText("진행 중")).toBeInTheDocument()
  })

  it("shows the token counts when there are any", () => {
    show({ runs: [run({ tokensIn: 120, tokensOut: 45 })] })

    expect(screen.getByText("토큰 120 → 45")).toBeInTheDocument()
  })

  it("says nothing about tokens for a node that uses none", () => {
    show()

    expect(screen.queryByText(/토큰/)).toBeNull()
  })

  it("says when a value was cut short", () => {
    show({ runs: [run({ truncated: true })] })

    expect(screen.getByText("값이 잘렸습니다")).toBeInTheDocument()
  })
})

describe("expanding a row", () => {
  it("cannot be expanded when there is nothing recorded", () => {
    show()

    expect(screen.getByRole("button")).toBeDisabled()
  })

  it("shows the input and output", async () => {
    show({ runs: [run({ input: { url: "https://example.com" }, output: { text: "응답" } })] })
    await userEvent.click(screen.getAllByRole("button")[0] as HTMLElement)

    expect(screen.getByText("입력")).toBeInTheDocument()
    expect(screen.getByText("https://example.com")).toBeInTheDocument()
    expect(screen.getByText("응답")).toBeInTheDocument()
  })

  it("never shows the redaction marker as plain text", async () => {
    // The whole reason the trace panel walks values instead of printing them.
    const { container } = show({
      runs: [run({ input: { headers: { Authorization: "[REDACTED]" } } })],
    })
    await userEvent.click(screen.getAllByRole("button")[0] as HTMLElement)

    expect(container.textContent).not.toContain("[REDACTED]")
    expect(screen.getByText("가려짐")).toBeInTheDocument()
  })

  it("translates a pydantic error message like everywhere else", async () => {
    show({ runs: [run({ status: "failed", error: { code: "INVALID_CONFIG", message: "설정 오류: Field required" } })] })
    await userEvent.click(screen.getAllByRole("button")[0] as HTMLElement)

    expect(screen.getByText("설정 오류: 반드시 입력해야 합니다")).toBeInTheDocument()
  })

  it("falls back to the error code when there is no message", async () => {
    show({ runs: [run({ status: "failed", error: { code: "HTTP_BLOCKED" } })] })
    await userEvent.click(screen.getAllByRole("button")[0] as HTMLElement)

    expect(screen.getByText("HTTP_BLOCKED")).toBeInTheDocument()
  })

  it("starts closed, so the panel is a list rather than a wall", async () => {
    show({ runs: [run({ output: { text: "응답" } })] })

    expect(screen.queryByText("출력")).toBeNull()
    await userEvent.click(screen.getAllByRole("button")[0] as HTMLElement)
    expect(screen.getByText("출력")).toBeInTheDocument()
  })
})

describe("more rows", () => {
  it("offers 더 보기 only when the engine says there are more", () => {
    const { unmount } = show({ hasMore: false })
    expect(screen.queryByRole("button", { name: "더 보기" })).toBeNull()
    unmount()

    show({ hasMore: true })
    expect(screen.getByRole("button", { name: "더 보기" })).toBeInTheDocument()
  })

  it("asks for them when pressed", async () => {
    const onLoadMore = vi.fn()
    show({ hasMore: true, onLoadMore })
    await userEvent.click(screen.getByRole("button", { name: "더 보기" }))

    expect(onLoadMore).toHaveBeenCalled()
  })

  it("does not ask twice while it is already loading", async () => {
    const onLoadMore = vi.fn()
    show({ hasMore: true, loading: true, onLoadMore })
    await userEvent.click(screen.getByRole("button", { name: "불러오는 중…" }))

    expect(onLoadMore).not.toHaveBeenCalled()
  })
})
