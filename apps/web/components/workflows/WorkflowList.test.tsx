import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { WorkflowSummary } from "@/lib/engine/workflows"

import { WorkflowList } from "./WorkflowList"

const push = vi.fn()
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }))

const ROW: WorkflowSummary = {
  id: "wf_1",
  name: "주문 처리",
  revision: 3,
  updatedAt: "2026-09-20T00:00:00.000Z",
}

let calls: { url: string; init?: RequestInit }[]

function answer(...responses: Response[]) {
  const queue = [...responses]
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init })
      return Promise.resolve(
        queue.shift() ?? new Response(JSON.stringify({ workflows: [ROW] }), { status: 200 }),
      )
    }),
  )
}

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })
}

beforeEach(() => {
  calls = []
  push.mockClear()
  answer()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("the list", () => {
  it("shows each workflow's name, revision and time in columns", () => {
    // A table, not cards: three facts per row in the same place each time is what makes a list
    // scannable down one column.
    render(<WorkflowList initial={[ROW]} />)

    expect(screen.getByRole("table")).toBeInTheDocument()
    expect(screen.getByText("주문 처리")).toBeInTheDocument()
    expect(screen.getByText("r3")).toBeInTheDocument()
  })

  it("says so when there is nothing yet, rather than showing an empty table", () => {
    render(<WorkflowList initial={[]} />)

    expect(screen.getByText(/아직 워크플로가 없습니다/)).toBeInTheDocument()
    expect(screen.queryByRole("table")).toBeNull()
  })

  it("opens a workflow by name", async () => {
    render(<WorkflowList initial={[ROW]} />)
    await userEvent.click(screen.getByRole("button", { name: "주문 처리" }))

    expect(push).toHaveBeenCalledWith("/workflows/wf_1")
  })
})

describe("creating", () => {
  it("makes one and goes straight to it", async () => {
    answer(json(201, { id: "wf_2", name: "새 워크플로", revision: 0 }))
    render(<WorkflowList initial={[]} />)
    await userEvent.click(screen.getByRole("button", { name: "새 워크플로" }))

    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({ name: "새 워크플로" })
    expect(push).toHaveBeenCalledWith("/workflows/wf_2")
  })

  it("stays put and says why when it fails", async () => {
    answer(json(503, { error: { message: "엔진에 연결할 수 없습니다" } }))
    render(<WorkflowList initial={[]} />)
    await userEvent.click(screen.getByRole("button", { name: "새 워크플로" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("엔진에 연결할 수 없습니다")
    expect(push).not.toHaveBeenCalled()
  })
})

describe("renaming", () => {
  it("sends the draft back with the new name, because PUT replaces the row", async () => {
    // Sending an empty document here would wipe the workflow. The rename is the smallest possible edit.
    const draft = { version: "1", nodes: [{ id: "start", type: "start", position: { x: 0, y: 0 } }], edges: [] }
    answer(
      json(200, { id: "wf_1", revision: 3, draftDsl: draft }),
      json(200, { revision: 4 }),
      json(200, { workflows: [{ ...ROW, name: "새 이름" }] }),
    )
    render(<WorkflowList initial={[ROW]} />)

    await userEvent.click(screen.getByRole("button", { name: "이름 바꾸기" }))
    const input = screen.getByLabelText("이름")
    await userEvent.clear(input)
    await userEvent.type(input, "새 이름{Enter}")

    const put = calls.find((call) => call.init?.method === "PUT")
    expect(JSON.parse(String(put?.init?.body))).toEqual({ name: "새 이름", draftDsl: draft, revision: 3 })
  })

  it("does nothing when the name is unchanged", async () => {
    render(<WorkflowList initial={[ROW]} />)
    await userEvent.click(screen.getByRole("button", { name: "이름 바꾸기" }))
    await userEvent.type(screen.getByLabelText("이름"), "{Enter}")

    expect(calls.some((call) => call.init?.method === "PUT")).toBe(false)
  })

  it("abandons the edit on Escape", async () => {
    render(<WorkflowList initial={[ROW]} />)
    await userEvent.click(screen.getByRole("button", { name: "이름 바꾸기" }))
    await userEvent.type(screen.getByLabelText("이름"), "무언가{Escape}")

    expect(screen.getByRole("button", { name: "주문 처리" })).toBeInTheDocument()
    expect(calls.some((call) => call.init?.method === "PUT")).toBe(false)
  })
})

describe("deleting", () => {
  it("asks first and says it cannot be undone", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false)
    render(<WorkflowList initial={[ROW]} />)
    await userEvent.click(screen.getByRole("button", { name: "삭제" }))

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("되돌릴 수 없습니다"))
    expect(calls.some((call) => call.init?.method === "DELETE")).toBe(false)
    confirm.mockRestore()
  })

  it("explains a refusal in the engine's words", async () => {
    // `WORKFLOW_HAS_ACTIVE_RUNS` is not a failure to retry: it is a refusal the tenant can act on.
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true)
    answer(json(409, { error: { code: "WORKFLOW_HAS_ACTIVE_RUNS", message: "실행 중인 워크플로는 삭제할 수 없습니다" } }))
    render(<WorkflowList initial={[ROW]} />)
    await userEvent.click(screen.getByRole("button", { name: "삭제" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("실행 중인 워크플로는 삭제할 수 없습니다")
    confirm.mockRestore()
  })
})
