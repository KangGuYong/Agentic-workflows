import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { MAX_IMPORT_BYTES } from "@/lib/dsl/transfer"
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

describe("import and export", () => {
  /** jsdom has no object URLs and no real downloads. Capturing the anchor is how the filename and the
   * blob's contents become observable -- both are what a person actually gets. */
  function captureDownload() {
    const saved: { name: string; text: Promise<string> }[] = []
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: (blob: Blob) => {
        saved.push({ name: "", text: blob.text() })
        return "blob:stub"
      },
      revokeObjectURL: () => {},
    })
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        const last = saved.at(-1)
        if (last !== undefined) last.name = this.download
      })
    return { saved, click }
  }

  function file(name: string, body: string) {
    return new File([body], name, { type: "application/json" })
  }

  const DSL = { version: "1", nodes: [{ id: "start", type: "start", position: { x: 0, y: 0 } }], edges: [] }

  it("creates a new workflow from a picked file and opens it", async () => {
    // Creating, never replacing: a file picker is one mis-click from the wrong file, and the worst
    // case here is one workflow to delete.
    answer(json(201, { id: "wf_new", name: "01-hello", revision: 1 }), json(200, { revision: 2 }))
    render(<WorkflowList initial={[ROW]} />)

    await userEvent.upload(screen.getByLabelText("워크플로 파일"), file("01-hello.json", JSON.stringify(DSL)))

    expect(calls[0]?.url).toBe("/api/engine/workflows")
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({ name: "01-hello" })
    expect(JSON.parse(String(calls[1]?.init?.body))).toMatchObject({ name: "01-hello", revision: 1 })
    expect(push).toHaveBeenCalledWith("/workflows/wf_new")
  })

  it("refuses an oversized file without reading it into memory", async () => {
    // The point of checking `File.size` when `parseImport` already counts bytes: a 200MB file must
    // never become a 200MB string first. Spying on `text()` is the only way to see that it did not.
    const read = vi.spyOn(File.prototype, "text")
    const huge = file("huge.json", "{}")
    Object.defineProperty(huge, "size", { value: MAX_IMPORT_BYTES + 1 })
    render(<WorkflowList initial={[ROW]} />)

    await userEvent.upload(screen.getByLabelText("워크플로 파일"), huge)

    expect(await screen.findByRole("alert")).toHaveTextContent("너무 큽니다")
    expect(read).not.toHaveBeenCalled()
    expect(calls).toHaveLength(0)
    read.mockRestore()
  })

  it("refuses a file that is not a workflow, and creates nothing", async () => {
    render(<WorkflowList initial={[ROW]} />)

    await userEvent.upload(screen.getByLabelText("워크플로 파일"), file("notes.json", "{ nope }"))

    expect(await screen.findByRole("alert")).toHaveTextContent("JSON 형식이 아닙니다")
    // The important half: a bad file must not leave an empty workflow behind.
    expect(calls).toHaveLength(0)
    expect(push).not.toHaveBeenCalled()
  })

  it("says the workflow was left empty when the document could not be saved", async () => {
    // The create succeeded and the save did not. Deleting it here would make an import that half
    // happened vanish; naming it is what lets someone find and remove it.
    answer(json(201, { id: "wf_new", name: "x", revision: 1 }), json(409, { error: { message: "충돌" } }))
    render(<WorkflowList initial={[ROW]} />)

    await userEvent.upload(screen.getByLabelText("워크플로 파일"), file("x.json", JSON.stringify(DSL)))

    expect(await screen.findByRole("alert")).toHaveTextContent("비어 있는 채로 만들어졌습니다")
    expect(push).not.toHaveBeenCalled()
  })

  it("downloads a row's stored draft under the workflow's name", async () => {
    const { saved, click } = captureDownload()
    answer(json(200, { id: "wf_1", name: "주문 처리", revision: 3, draftDsl: DSL }))
    render(<WorkflowList initial={[ROW]} />)

    await userEvent.click(screen.getByRole("button", { name: "내보내기" }))

    expect(click).toHaveBeenCalled()
    expect(saved[0]?.name).toBe("주문 처리.json")
    expect(JSON.parse(await (saved[0]?.text ?? Promise.resolve("null")))).toEqual(DSL)
  })

  it("writes the file indented, so an exported workflow is one someone can read", async () => {
    const { saved } = captureDownload()
    answer(json(200, { id: "wf_1", name: "주문 처리", revision: 3, draftDsl: DSL }))
    render(<WorkflowList initial={[ROW]} />)

    await userEvent.click(screen.getByRole("button", { name: "내보내기" }))

    expect(await (saved[0]?.text ?? Promise.resolve(""))).toContain('\n  "nodes"')
  })

  it("reports a draft it could not fetch instead of saving an empty file", async () => {
    const { click } = captureDownload()
    answer(json(500, { error: { message: "엔진에 연결할 수 없습니다" } }))
    render(<WorkflowList initial={[ROW]} />)

    await userEvent.click(screen.getByRole("button", { name: "내보내기" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("엔진에 연결할 수 없습니다")
    expect(click).not.toHaveBeenCalled()
  })
})
