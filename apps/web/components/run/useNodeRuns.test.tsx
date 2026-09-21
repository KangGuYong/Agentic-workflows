import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { NodeRun } from "@/lib/run/trace"

import { PAGE_SIZE, useNodeRuns } from "./useNodeRuns"

function row(over: Partial<NodeRun> = {}): NodeRun {
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
    finishedAt: "2026-09-21T04:00:01.000Z",
    ...over,
  }
}

let calls: string[]

function serve(...pages: ({ nodeRuns: NodeRun[]; hasMore: boolean } | Error)[]) {
  const queue = [...pages]
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      calls.push(url)
      const next = queue.shift() ?? { nodeRuns: [], hasMore: false }
      if (next instanceof Error) return Promise.reject(next)
      return Promise.resolve(
        new Response(JSON.stringify(next), { status: 200, headers: { "content-type": "application/json" } }),
      )
    }),
  )
}

function Probe({ runId, finished }: { runId: string | null; finished: boolean }) {
  const trace = useNodeRuns(runId, finished)
  return (
    <div>
      <span data-testid="count">{trace.runs.length}</span>
      <span data-testid="loading">{String(trace.loading)}</span>
      <span data-testid="more">{String(trace.hasMore)}</span>
      <span data-testid="error">{trace.error ?? ""}</span>
      <button type="button" onClick={trace.loadMore}>
        더
      </button>
    </div>
  )
}

beforeEach(() => {
  calls = []
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("useNodeRuns", () => {
  it("fetches nothing without a run", () => {
    serve()
    render(<Probe runId={null} finished={false} />)

    expect(calls).toHaveLength(0)
    expect(screen.getByTestId("loading").textContent).toBe("false")
  })

  it("fetches a page for the run, with the default limit", async () => {
    serve({ nodeRuns: [row(), row({ attempt: 2 })], hasMore: false })
    render(<Probe runId="r1" finished={false} />)

    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("2"))
    expect(calls[0]).toBe(`/api/engine/runs/r1/nodes?limit=${PAGE_SIZE}`)
  })

  it("fetches again when the run ends, because that is when the record is complete", async () => {
    serve({ nodeRuns: [row()], hasMore: false }, { nodeRuns: [row(), row({ attempt: 2 })], hasMore: false })
    const { rerender } = render(<Probe runId="r1" finished={false} />)
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("1"))

    rerender(<Probe runId="r1" finished={true} />)
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("2"))
  })

  it("does not fetch per render", async () => {
    // The stream drives the live status; refetching on every frame would be a request per token.
    serve({ nodeRuns: [row()], hasMore: false })
    const { rerender } = render(<Probe runId="r1" finished={false} />)
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("1"))
    rerender(<Probe runId="r1" finished={false} />)
    rerender(<Probe runId="r1" finished={false} />)

    expect(calls).toHaveLength(1)
  })

  it("asks for a bigger page on 더 보기", async () => {
    serve({ nodeRuns: [row()], hasMore: true }, { nodeRuns: [row(), row({ attempt: 2 })], hasMore: false })
    render(<Probe runId="r1" finished={false} />)
    await waitFor(() => expect(screen.getByTestId("more").textContent).toBe("true"))

    await userEvent.click(screen.getByRole("button", { name: "더" }))
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("2"))
    expect(calls[1]).toBe(`/api/engine/runs/r1/nodes?limit=${PAGE_SIZE * 2}`)
  })

  it("keeps the rows on screen while the bigger page is on its way", async () => {
    // 더 보기 should extend the list, not blank it and then refill it.
    serve({ nodeRuns: [row()], hasMore: true }, { nodeRuns: [row(), row({ attempt: 2 })], hasMore: false })
    render(<Probe runId="r1" finished={false} />)
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("1"))

    await userEvent.click(screen.getByRole("button", { name: "더" }))
    expect(Number(screen.getByTestId("count").textContent)).toBeGreaterThan(0)
  })

  it("reports a failure and keeps whatever was already shown", async () => {
    serve({ nodeRuns: [row()], hasMore: false }, new Error("실행 기록을 불러오지 못했습니다 (502)"))
    const { rerender } = render(<Probe runId="r1" finished={false} />)
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("1"))

    rerender(<Probe runId="r1" finished={true} />)
    await waitFor(() => expect(screen.getByTestId("error").textContent).toMatch(/502/))
    // A failed refresh is not evidence the history is gone.
    expect(screen.getByTestId("count").textContent).toBe("1")
  })

  it("reports nothing at all once there is no run", async () => {
    serve({ nodeRuns: [row()], hasMore: true })
    const { rerender } = render(<Probe runId="r1" finished={false} />)
    await waitFor(() => expect(screen.getByTestId("count").textContent).toBe("1"))

    rerender(<Probe runId={null} finished={false} />)
    expect(screen.getByTestId("count").textContent).toBe("0")
    expect(screen.getByTestId("more").textContent).toBe("false")
  })
})
