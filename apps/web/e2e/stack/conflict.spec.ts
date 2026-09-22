import { expect, test } from "@playwright/test"

import { createWorkflow, deleteWorkflow, dropNode, editorPath, endNode, pressSave, saveNow, startNode } from "./support"

/** Two people editing the same workflow, and the one who saves second (3 설계 §9, Task 21).
 *
 * Two browser contexts rather than two tabs of one: separate contexts have separate storage, which is
 * what makes this two *editors* rather than one editor talking to itself.
 */

let workflowId: string

test.beforeEach(async ({ request }) => {
  const workflow = await createWorkflow(request, `e2e-conflict-${Date.now()}`, {
    version: "1",
    nodes: [startNode({ name: { type: "string" } }), endNode({ greeting: "{{ start.name }}" })],
    edges: [{ id: "edge_1", source: "start", target: "end" }],
  })
  workflowId = workflow.id
})

test.afterEach(async ({ request }) => {
  await deleteWorkflow(request, workflowId)
})

test("the second save is refused, and 불러오기 replaces the document", async ({ browser }) => {
  const first = await browser.newContext()
  const second = await browser.newContext()
  try {
    const a = await first.newPage()
    const b = await second.newPage()

    // Both open **before** either saves: the second editor has to be holding the old revision for the
    // conflict to be the one this tests, rather than a stale page that never loaded.
    await a.goto(editorPath(workflowId))
    await b.goto(editorPath(workflowId))
    await expect(a.locator(".react-flow__node")).toHaveCount(2, { timeout: 20_000 })
    await expect(b.locator(".react-flow__node")).toHaveCount(2, { timeout: 20_000 })

    // Two different node types, so "whose document won" is answerable by looking at the canvas.
    await dropNode(a, "템플릿", { x: 260, y: 220 })
    await saveNow(a)

    await dropNode(b, "사람 승인", { x: 260, y: 220 })
    await pressSave(b)

    const dialog = b.getByRole("dialog")
    await expect(dialog).toBeVisible({ timeout: 20_000 })
    await expect(dialog).toContainText("다른 곳에서 변경됨")
    // The cost of each choice is on the button. Asserting it keeps a redesign that drops the warning
    // from passing silently.
    await expect(dialog).toContainText("이 화면에서 한 편집은 사라집니다")

    await dialog.getByRole("button", { name: /저장된 내용 불러오기/ }).click()

    await expect(dialog).toBeHidden({ timeout: 20_000 })
    // The other editor's node is here and this editor's is gone -- which is what "불러오기" costs, and
    // the count alone would not distinguish it from nothing having happened.
    await expect(b.locator(".react-flow__node", { hasText: "템플릿" })).toHaveCount(1, { timeout: 20_000 })
    await expect(b.locator(".react-flow__node", { hasText: "사람 승인" })).toHaveCount(0)
    await expect(b.locator(".react-flow__node")).toHaveCount(3)
  } finally {
    await first.close()
    await second.close()
  }
})
