import { describe, expect, it, vi, beforeEach, afterEach } from "vitest"

import { MAX_IMPORT_BYTES } from "@/lib/dsl/transfer"

import { importWorkflowFile } from "./import"

const DSL = { version: "1", nodes: [{ id: "start", type: "start", position: { x: 0, y: 0 } }], edges: [] }

let calls: { url: string; init?: RequestInit }[]

function answer(...responses: Response[]) {
  const queue = [...responses]
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init })
      return Promise.resolve(queue.shift() ?? new Response("{}", { status: 200 }))
    }),
  )
}

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })
}

function file(name: string, body: string, size?: number) {
  const made = new File([body], name, { type: "application/json" })
  if (size !== undefined) Object.defineProperty(made, "size", { value: size })
  return made
}

beforeEach(() => {
  calls = []
  answer()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("importWorkflowFile", () => {
  it("creates a workflow named after the file and saves the document into it", async () => {
    answer(json(201, { id: "wf_new", name: "01-hello", revision: 1 }), json(200, { revision: 2 }))

    const result = await importWorkflowFile(file("01-hello.json", JSON.stringify(DSL)))

    expect(result).toEqual({ outcome: "ok", id: "wf_new", name: "01-hello" })
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({ name: "01-hello" })
    expect(JSON.parse(String(calls[1]?.init?.body))).toMatchObject({ name: "01-hello", revision: 1 })
  })

  it("refuses a file that is not a workflow before creating anything", async () => {
    const result = await importWorkflowFile(file("junk.json", "{ nope }"))

    expect(result).toEqual({ outcome: "rejected", message: "JSON 형식이 아닙니다." })
    expect(calls).toHaveLength(0)
  })

  it("refuses an oversized file without reading it", async () => {
    // `File.size` is checked before `text()` so a huge file never becomes a huge string.
    const read = vi.spyOn(File.prototype, "text")

    const result = await importWorkflowFile(file("huge.json", "{}", MAX_IMPORT_BYTES + 1))

    expect(result.outcome).toBe("rejected")
    expect(read).not.toHaveBeenCalled()
    expect(calls).toHaveLength(0)
    read.mockRestore()
  })

  it("reports the workflow it left behind when the document could not be saved", async () => {
    // Not deleted: an import that half happened must not vanish, and the name is how it is found.
    answer(json(201, { id: "wf_new", name: "x", revision: 1 }), json(409, { error: { message: "충돌" } }))

    const result = await importWorkflowFile(file("x.json", JSON.stringify(DSL)))

    expect(result).toMatchObject({ outcome: "partial", id: "wf_new", name: "x" })
    expect(result.outcome === "partial" && result.message).toContain("비어 있는 채로 만들어졌습니다")
  })

  it("reports a failed create as a plain rejection, with nothing left behind", async () => {
    answer(json(500, { error: { message: "엔진에 연결할 수 없습니다" } }))

    expect(await importWorkflowFile(file("x.json", JSON.stringify(DSL)))).toEqual({
      outcome: "rejected",
      message: "엔진에 연결할 수 없습니다",
    })
  })

  it("fills in positions a hand-written file has none of", async () => {
    answer(json(201, { id: "wf_new", name: "bare", revision: 1 }), json(200, { revision: 2 }))

    await importWorkflowFile(file("bare.json", JSON.stringify({ version: "1", nodes: [{ id: "a", type: "start" }], edges: [] })))

    const sent = JSON.parse(String(calls[1]?.init?.body)) as { draftDsl: { nodes: { position: unknown }[] } }
    expect(sent.draftDsl.nodes[0]?.position).toEqual({ x: 80, y: 80 })
  })
})
