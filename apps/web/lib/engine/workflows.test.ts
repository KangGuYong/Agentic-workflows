import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { createWorkflow, deleteWorkflow, listWorkflows, putWorkflow } from "./workflows"

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

const ROW = { id: "wf_1", name: "주문 처리", revision: 3, updatedAt: "2026-09-21T04:00:00.000Z" }

describe("listWorkflows", () => {
  it("reads the list through the proxy", async () => {
    answer(reply(200, { workflows: [ROW] }))

    expect(await listWorkflows()).toEqual({ outcome: "ok", workflows: [ROW] })
    expect(calls[0]?.url).toBe("/api/engine/workflows")
  })

  it("drops a row it cannot read rather than failing the whole list", async () => {
    // One malformed row should not take the other nine off the screen.
    answer(reply(200, { workflows: [ROW, { id: "wf_2" }, null, "x"] }))

    expect(await listWorkflows()).toEqual({ outcome: "ok", workflows: [ROW] })
  })

  it("reports a failure rather than an empty list", async () => {
    // An empty list means "you have no workflows", which is a different thing to see.
    answer(reply(502, { error: { message: "게이트웨이 오류" } }))

    expect(await listWorkflows()).toEqual({ outcome: "failed", message: "게이트웨이 오류" })
  })

  it("fails on a body that is not the contract", async () => {
    answer(reply(200, { items: [] }))

    expect(await listWorkflows()).toMatchObject({ outcome: "failed" })
  })

  it("reads a row with no updatedAt", async () => {
    answer(reply(200, { workflows: [{ id: "wf_1", name: "x", revision: 0 }] }))

    expect(await listWorkflows()).toMatchObject({ outcome: "ok", workflows: [{ updatedAt: undefined }] })
  })
})

describe("createWorkflow", () => {
  it("POSTs the name and reports what was made", async () => {
    answer(reply(201, ROW))

    expect(await createWorkflow("주문 처리")).toEqual({ outcome: "ok", value: ROW })
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({ name: "주문 처리" })
  })

  it("passes the engine's refusal through", async () => {
    answer(reply(422, { error: { message: "이름이 너무 깁니다" } }))

    expect(await createWorkflow("x".repeat(500))).toEqual({ outcome: "failed", message: "이름이 너무 깁니다" })
  })
})

describe("putWorkflow", () => {
  it("PUTs the name with the draft and revision, because PUT replaces the row", async () => {
    const draft = { version: "1", nodes: [], edges: [] }
    answer(reply(200, { revision: 4 }))

    expect(await putWorkflow("wf_1", "새 이름", draft, 3)).toEqual({ outcome: "ok", value: 4 })
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({ name: "새 이름", draftDsl: draft, revision: 3 })
  })

  it("reports a 409 as blocked, not as a failure to retry", async () => {
    answer(reply(409, { error: { code: "REVISION_CONFLICT", message: "다른 곳에서 먼저 저장했습니다" } }))

    expect(await putWorkflow("wf_1", "x", {}, 3)).toEqual({
      outcome: "blocked",
      message: "다른 곳에서 먼저 저장했습니다",
    })
  })

  it("escapes the id", async () => {
    answer(reply(200, { revision: 1 }))
    await putWorkflow("../secrets", "x", {}, 0)

    expect(calls[0]?.url).toBe("/api/engine/workflows/..%2Fsecrets")
  })
})

describe("deleteWorkflow", () => {
  it("reports success on the engine's 204", async () => {
    answer(new Response(null, { status: 204 }))

    expect(await deleteWorkflow("wf_1")).toEqual({ outcome: "ok" })
    expect(calls[0]?.init?.method).toBe("DELETE")
  })

  it("reports an active run as blocked with the engine's reason", async () => {
    // Not a failure to retry: it is a refusal the tenant can act on -- wait, or cancel the run.
    answer(reply(409, { error: { code: "WORKFLOW_HAS_ACTIVE_RUNS", message: "실행 중인 워크플로는 삭제할 수 없습니다" } }))

    expect(await deleteWorkflow("wf_1")).toEqual({
      outcome: "blocked",
      message: "실행 중인 워크플로는 삭제할 수 없습니다",
    })
  })

  it("reports anything else as a failure", async () => {
    answer(reply(500, null))

    expect(await deleteWorkflow("wf_1")).toMatchObject({ outcome: "failed" })
  })
})
