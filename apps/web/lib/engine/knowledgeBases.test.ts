import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { createKnowledgeBase, deleteFile, deleteKnowledgeBase, listFiles, listKnowledgeBases, uploadFile } from "./knowledgeBases"

function reply(status: number, body?: unknown) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  })
}

let calls: { url: string; init?: RequestInit }[]

function answer(...responses: Response[]) {
  const queue = [...responses]
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init })
      return Promise.resolve(queue.shift() ?? reply(500, null))
    }),
  )
}

beforeEach(() => {
  calls = []
})

afterEach(() => {
  vi.unstubAllGlobals()
})

const KB = { id: "kb_1", name: "문서", embedModel: "bge-m3", fileCount: 2, createdAt: "2026-09-22T00:00:00.000Z" }
const FILE = { id: "f_1", filename: "a.md", size: 12, status: "pending", error: null, createdAt: "2026-09-22T00:00:00.000Z", updatedAt: "2026-09-22T00:00:00.000Z" }

describe("listKnowledgeBases", () => {
  it("reads the list through the proxy", async () => {
    answer(reply(200, { knowledgeBases: [KB] }))
    expect(await listKnowledgeBases()).toEqual({ outcome: "ok", knowledgeBases: [KB] })
    expect(calls[0]?.url).toBe("/api/engine/knowledge-bases")
  })

  it("passes the engine's message through on failure", async () => {
    answer(reply(503, { error: { code: "INTERNAL", message: "엔진 점검 중" } }))
    expect(await listKnowledgeBases()).toEqual({ outcome: "failed", message: "엔진 점검 중" })
  })

  it("drops a row it cannot read rather than rendering it half-blank", async () => {
    answer(reply(200, { knowledgeBases: [KB, { id: "x" }] }))
    expect(await listKnowledgeBases()).toEqual({ outcome: "ok", knowledgeBases: [KB] })
  })
})

describe("createKnowledgeBase", () => {
  it("posts the name and returns the row", async () => {
    answer(reply(201, KB))
    expect(await createKnowledgeBase("문서")).toEqual({ outcome: "ok", knowledgeBase: KB })
    expect(calls[0]?.init?.method).toBe("POST")
    expect(JSON.parse(calls[0]?.init?.body as string)).toEqual({ name: "문서" })
  })

  it("reports an unreadable 2xx body instead of rendering it", async () => {
    answer(reply(201, {}))
    expect(await createKnowledgeBase("문서")).toEqual({ outcome: "failed", message: "지식베이스 응답을 이해하지 못했습니다" })
  })
})

describe("deleteKnowledgeBase", () => {
  it("succeeds on 204", async () => {
    answer(reply(204))
    expect(await deleteKnowledgeBase("kb_1")).toEqual({ outcome: "ok" })
  })

  it("passes the engine's message through on 404", async () => {
    answer(reply(404, { error: { code: "NOT_FOUND", message: "지식베이스를 찾을 수 없습니다" } }))
    expect(await deleteKnowledgeBase("kb_1")).toEqual({ outcome: "failed", message: "지식베이스를 찾을 수 없습니다" })
  })
})

describe("uploadFile", () => {
  it("PUTs the raw file with its name in the query and its type in the header", async () => {
    answer(reply(202, FILE))
    const file = new File(["# a"], "보고서.md", { type: "text/markdown" })

    expect(await uploadFile("kb_1", file)).toEqual({ outcome: "ok", file: FILE })
    expect(calls[0]?.url).toBe("/api/engine/knowledge-bases/kb_1/files?name=%EB%B3%B4%EA%B3%A0%EC%84%9C.md")
    expect(calls[0]?.init?.method).toBe("PUT")
    expect((calls[0]?.init?.headers as Record<string, string>)["content-type"]).toBe("text/markdown")
    expect(calls[0]?.init?.body).toBe(file)
  })

  it("falls back to octet-stream when the browser knows no type", async () => {
    answer(reply(202, FILE))
    await uploadFile("kb_1", new File(["x"], "a.hwp"))
    expect((calls[0]?.init?.headers as Record<string, string>)["content-type"]).toBe("application/octet-stream")
  })

  it("reports a 413 in the engine's words", async () => {
    answer(reply(413, { error: { code: "PAYLOAD_TOO_LARGE", message: "파일이 너무 큽니다 (최대 50MB)" } }))
    expect(await uploadFile("kb_1", new File(["x"], "a.md"))).toEqual({ outcome: "failed", message: "파일이 너무 큽니다 (최대 50MB)" })
  })
})

describe("listFiles and deleteFile", () => {
  it("reads and deletes under the knowledge base", async () => {
    answer(reply(200, { files: [FILE] }), reply(204))
    expect(await listFiles("kb_1")).toEqual({ outcome: "ok", files: [FILE] })
    expect(await deleteFile("kb_1", "f_1")).toEqual({ outcome: "ok" })
    expect(calls[1]?.url).toBe("/api/engine/knowledge-bases/kb_1/files/f_1")
    expect(calls[1]?.init?.method).toBe("DELETE")
  })

  it("drops a file row with an unrecognized status", async () => {
    answer(reply(200, { files: [FILE, { ...FILE, id: "f_2", status: "weird" }] }))
    expect(await listFiles("kb_1")).toEqual({ outcome: "ok", files: [FILE] })
  })
})
