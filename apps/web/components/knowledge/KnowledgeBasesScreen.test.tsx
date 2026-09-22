import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { KbFile, KnowledgeBaseSummary } from "@/lib/engine/knowledgeBases"

import { KnowledgeBasesScreen } from "./KnowledgeBasesScreen"

const KB: KnowledgeBaseSummary = { id: "kb_1", name: "제품 문서", embedModel: "bge-m3", fileCount: 1, createdAt: "2026-09-22T00:00:00.000Z" }
const READY: KbFile = { id: "f_1", filename: "guide.pdf", size: 2048, status: "ready", error: null, createdAt: "2026-09-22T00:00:00.000Z", updatedAt: "2026-09-22T00:00:00.000Z" }
const PENDING: KbFile = { ...READY, id: "f_2", filename: "new.md", status: "pending" }
const FAILED: KbFile = { ...READY, id: "f_3", filename: "bad.xyz", status: "failed", error: "문서를 읽지 못했습니다 (MinerU 422)" }

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })
}

let calls: { url: string; init?: RequestInit }[]
let fetchMock: ReturnType<typeof vi.fn>

function answer(route: (url: string, init?: RequestInit) => Response) {
  fetchMock = vi.fn((url: string, init?: RequestInit) => {
    calls.push({ url, init })
    return Promise.resolve(route(url, init))
  })
  vi.stubGlobal("fetch", fetchMock)
}

beforeEach(() => {
  calls = []
  answer((url) => (url.endsWith("/files") ? json(200, { files: [READY] }) : json(200, { knowledgeBases: [KB] })))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe("the list", () => {
  it("shows every knowledge base with its file count", () => {
    render(<KnowledgeBasesScreen initial={[KB]} />)
    expect(screen.getByRole("button", { name: /제품 문서/ })).toHaveTextContent("파일 1개")
  })

  it("creates one by name and reloads the list", async () => {
    answer((url, init) =>
      init?.method === "POST" ? json(201, { ...KB, id: "kb_2", name: "새 문서" }) : json(200, { knowledgeBases: [KB, { ...KB, id: "kb_2", name: "새 문서" }] }),
    )
    render(<KnowledgeBasesScreen initial={[KB]} />)
    await userEvent.type(screen.getByLabelText("새 지식베이스 이름"), "새 문서")
    await userEvent.click(screen.getByRole("button", { name: "만들기" }))
    await waitFor(() => expect(screen.getByRole("button", { name: /새 문서/ })).toBeInTheDocument())
  })
})

describe("files of the open knowledge base", () => {
  it("lists them with status, and a failed file shows why", async () => {
    answer((url) => (url.endsWith("/files") ? json(200, { files: [READY, FAILED] }) : json(200, { knowledgeBases: [KB] })))
    render(<KnowledgeBasesScreen initial={[KB]} />)
    await userEvent.click(screen.getByRole("button", { name: /제품 문서/ }))

    expect(await screen.findByText("guide.pdf")).toBeInTheDocument()
    expect(screen.getByText("완료")).toBeInTheDocument()
    expect(screen.getByText("문서를 읽지 못했습니다 (MinerU 422)")).toBeInTheDocument()
  })

  it("uploads each chosen file and reloads the list", async () => {
    answer((url, init) => {
      if (init?.method === "PUT") return json(202, PENDING)
      if (url.endsWith("/files")) return json(200, { files: [READY, PENDING] })
      return json(200, { knowledgeBases: [KB] })
    })
    render(<KnowledgeBasesScreen initial={[KB]} />)
    await userEvent.click(screen.getByRole("button", { name: /제품 문서/ }))
    await screen.findByText("guide.pdf")

    await userEvent.upload(screen.getByLabelText("파일 올리기"), new File(["# n"], "new.md", { type: "text/markdown" }))

    await waitFor(() => expect(screen.getByText("new.md")).toBeInTheDocument())
    expect(calls.some((c) => c.init?.method === "PUT" && c.url.includes("name=new.md"))).toBe(true)
  })

  it("polls every 5 seconds only while a file is pending or processing", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let phase = 0
    answer((url) => {
      if (!url.endsWith("/files")) return json(200, { knowledgeBases: [KB] })
      phase += 1
      return json(200, { files: [phase < 3 ? PENDING : READY] })
    })
    render(<KnowledgeBasesScreen initial={[KB]} />)
    await userEvent.click(screen.getByRole("button", { name: /제품 문서/ }))
    await screen.findByText("대기 중")

    await vi.advanceTimersByTimeAsync(5_100)
    await vi.advanceTimersByTimeAsync(5_100)
    await screen.findByText("완료")
    const after = calls.filter((c) => c.url.endsWith("/files")).length
    await vi.advanceTimersByTimeAsync(11_000)
    expect(calls.filter((c) => c.url.endsWith("/files")).length).toBe(after)
  })
})
