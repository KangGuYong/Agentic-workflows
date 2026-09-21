import { expect, test } from "@playwright/test"

import {
  connect,
  createWorkflow,
  deleteWorkflow,
  deselect,
  dropNode,
  editorPath,
  endNode,
  expectRunStatus,
  expectSaved,
  startNode,
} from "./support"

/** Building a workflow in the editor and running it (3 설계 §11, Task 21).
 *
 * The one scenario that touches everything: dropping a node, drawing an edge, completing a reference
 * from the engine's own analysis, autosaving, validating, running, streaming, and reading the result
 * back out of the trace. If this passes, the pieces are connected to each other and not only to their
 * own tests.
 */

let workflowId: string

test.beforeEach(async ({ request }) => {
  // Start and end only: the template node and both edges are what the test draws.
  const workflow = await createWorkflow(request, `e2e-build-${Date.now()}`, {
    version: "1",
    nodes: [startNode({ name: { type: "string" } }), endNode({ greeting: "{{ template_1.text }}" })],
    edges: [],
  })
  workflowId = workflow.id
})

test.afterEach(async ({ request }) => {
  await deleteWorkflow(request, workflowId)
})

test("draw a template node, wire it up, and run it", async ({ page }) => {
  await page.goto(editorPath(workflowId))
  await expect(page.locator(".react-flow__node")).toHaveCount(2, { timeout: 20_000 })

  // 1. Drop a template node from the palette. Dropping selects it, which opens the node panel over the
  //    right third of the canvas -- including the end node -- so the selection is cleared before
  //    anything is dragged.
  await dropNode(page, "템플릿", { x: 300, y: 240 })
  await deselect(page)

  // 2. Wire start → template → end by dragging between handles.
  await connect(page, "start", "template_1", 1)
  await connect(page, "template_1", "end", 2)

  // 3. Fill the template, choosing the reference from autocomplete rather than typing it.
  await page.locator(".react-flow__node", { hasText: "템플릿" }).click()
  const editor = page.locator("[data-testid='template-editor'] .cm-content")
  await editor.click()
  await page.keyboard.type("안녕, {{ 시")
  const option = page.locator(".cm-tooltip-autocomplete li", { hasText: "시작" })
  await expect(option).toBeVisible({ timeout: 10_000 })
  await option.click()
  await page.keyboard.type(".name }}!")

  await expectSaved(page)

  // 4. Validation has to clear before the run button will do anything.
  const run = page.getByRole("button", { name: "실행" })
  await expect(run).toBeEnabled({ timeout: 20_000 })
  await run.click()

  await page.getByLabel("name").fill("세계")
  await page.getByRole("button", { name: "실행", exact: true }).last().click()

  // 5. The run streams to success, and the node shows it.
  await expectRunStatus(page, /성공/)
  await expect(
    page.locator(".react-flow__node", { hasText: "템플릿" }).getByRole("img", { name: /실행 상태/ }),
  ).toHaveAccessibleName("실행 상태: 성공")

  // 6. The rendered text is in the trace, which is where a person would look for it.
  await page.getByRole("tab", { name: "실행 기록" }).click()
  await page.getByRole("button", { name: /시도/ }).first().click()
  await expect(page.getByRole("complementary", { name: "노드 설정" })).toContainText("안녕, 세계!", {
    timeout: 10_000,
  })
})
