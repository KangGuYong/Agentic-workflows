import { expect, test } from "@playwright/test"

import { createWorkflow, deleteWorkflow, editorPath, endNode, expectRunStatus, startNode } from "./support"

/** Upload a markdown file, wait for ingestion, and search it from a workflow (knowledge-base design §8).
 *
 * Markdown skips MinerU, so this runs against the compose stack alone: postgres (pgvector), api, worker,
 * ingester, web, and the Ollama host the stack already points at for the embedding.
 */

let kbId: string
let workflowId: string

test.beforeEach(async ({ request }) => {
  const created = await request.post("/api/engine/knowledge-bases", { data: { name: `e2e-kb-${Date.now()}` } })
  expect(created.status(), await created.text()).toBe(201)
  kbId = ((await created.json()) as { id: string }).id

  const uploaded = await request.put(`/api/engine/knowledge-bases/${kbId}/files?name=policy.md`, {
    headers: { "content-type": "text/markdown" },
    data: "# 환불 정책\n\n구매 후 14일 이내에는 전액 환불됩니다.\n\n# 배송\n\n주문 후 3일 안에 발송합니다.\n",
  })
  expect(uploaded.status(), await uploaded.text()).toBe(202)

  await expect
    .poll(async () => {
      const listed = await request.get(`/api/engine/knowledge-bases/${kbId}/files`)
      const { files } = (await listed.json()) as { files: { status: string; error: string | null }[] }
      if (files[0]?.status === "failed") throw new Error(`ingestion failed: ${files[0].error}`)
      return files[0]?.status
    }, { timeout: 60_000, intervals: [1_000] })
    .toBe("ready")

  const workflow = await createWorkflow(request, `e2e-kb-${Date.now()}`, {
    version: "1",
    nodes: [
      startNode({ question: { type: "string" } }),
      {
        id: "kb_search_1",
        type: "kb_search",
        position: { x: 340, y: 80 },
        config: { knowledgeBase: kbId, query: "{{ start.question }}", topK: 1 },
      },
      endNode({ answer: "{{ kb_search_1.context }}" }),
    ],
    edges: [
      { id: "edge_1", source: "start", target: "kb_search_1" },
      { id: "edge_2", source: "kb_search_1", target: "end" },
    ],
  })
  workflowId = workflow.id
})

test.afterEach(async ({ request }) => {
  await deleteWorkflow(request, workflowId)
  await request.delete(`/api/engine/knowledge-bases/${kbId}`).catch(() => undefined)
})

test("a question finds the matching section of an uploaded file", async ({ page, request }) => {
  await page.goto(editorPath(workflowId))
  await expect(page.locator(".react-flow__node")).toHaveCount(3, { timeout: 20_000 })

  const run = page.getByRole("button", { name: "실행" })
  await expect(run).toBeEnabled({ timeout: 20_000 })
  await run.click()
  await page.getByLabel("question").fill("환불은 언제까지 되나요?")
  await page.getByRole("button", { name: "실행", exact: true }).last().click()
  await expectRunStatus(page, /성공/, 60_000)

  const listed = await request.get(`/api/engine/workflows/${workflowId}/runs`)
  const { runs } = (await listed.json()) as { runs: { id: string }[] }
  const detail = await request.get(`/api/engine/runs/${runs[0]!.id}`)
  const { outputs } = (await detail.json()) as { outputs: { answer: string } }
  expect(outputs.answer).toContain("14일")
  expect(outputs.answer).not.toContain("배송")
})
