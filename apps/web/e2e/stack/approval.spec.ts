import { expect, test } from "@playwright/test"

import {
  approvalWorkflow,
  cancelRuns,
  createWorkflow,
  deleteWorkflow,
  editorPath,
  expectRunStatus,
} from "./support"

/** A run that stops for a person, and the person answering it (3 설계 §8.4, Task 21).
 *
 * The graph is made through the API rather than drawn: what this scenario is about is the interrupt,
 * the dialog, the resume and the decision landing in the trace, and drawing three edges first would
 * only repeat `build-and-run`'s coverage while giving this one more ways to fail for reasons that have
 * nothing to do with approval.
 */

let workflowId: string

test.beforeEach(async ({ request }) => {
  const workflow = await createWorkflow(request, `e2e-approval-${Date.now()}`, approvalWorkflow())
  workflowId = workflow.id
})

test.afterEach(async ({ request }) => {
  // A parked run holds the workflow: the engine refuses to delete one with a run still waiting.
  await cancelRuns(request, workflowId)
  await deleteWorkflow(request, workflowId)
})

test("a parked run is answered from the dialog and the decision reaches the trace", async ({ page }) => {
  await page.goto(editorPath(workflowId))
  await expect(page.locator(".react-flow__node")).toHaveCount(3, { timeout: 20_000 })

  const run = page.getByRole("button", { name: "실행" })
  await expect(run).toBeEnabled({ timeout: 20_000 })
  await run.click()
  await page.getByLabel("item").fill("커피")
  await page.getByRole("button", { name: "실행", exact: true }).last().click()

  // The run parks, and the dialog carries the node's own message and the value it asked about.
  const dialog = page.getByRole("dialog")
  await expect(dialog).toBeVisible({ timeout: 30_000 })
  await expect(dialog).toContainText("이 주문을 승인해 주세요")
  await expect(dialog).toContainText("커피")
  await expectRunStatus(page, /승인 대기/)

  await dialog.getByLabel("의견 (선택)").fill("확인했습니다")
  await dialog.getByRole("button", { name: "승인" }).click()

  await expect(dialog).toBeHidden({ timeout: 20_000 })
  await expectRunStatus(page, /성공/)

  // The decision is in the node's trace, which is the record a reviewer goes back to.
  await page.locator(".react-flow__node", { hasText: "사람 승인" }).click()
  await page.getByRole("tab", { name: "실행 기록" }).click()
  await page.getByRole("button", { name: /시도/ }).first().click()
  const panel = page.getByRole("complementary", { name: "노드 설정" })
  await expect(panel).toContainText("approve", { timeout: 10_000 })
  await expect(panel).toContainText("확인했습니다")
})
