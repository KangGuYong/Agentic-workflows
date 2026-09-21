import { expect, test } from "@playwright/test"

import {
  approvalWorkflow,
  cancelRuns,
  createWorkflow,
  deleteWorkflow,
  editorPath,
  expectRunStatus,
} from "./support"

/** A parked run survives the browser (3 설계 §8.5, Task 21).
 *
 * The run lives in the engine, not in the tab, and this is the scenario that says so: a person who
 * closes the laptop and comes back has to find the same question waiting, with the canvas coloured the
 * way they left it. Nothing here is restored from browser storage -- the editor asks the engine.
 */

let workflowId: string

test.beforeEach(async ({ request }) => {
  const workflow = await createWorkflow(request, `e2e-reload-${Date.now()}`, approvalWorkflow())
  workflowId = workflow.id
})

test.afterEach(async ({ request }) => {
  await cancelRuns(request, workflowId)
  await deleteWorkflow(request, workflowId)
})

test("reloading the page finds the same approval waiting and the canvas coloured", async ({ page }) => {
  await page.goto(editorPath(workflowId))
  await expect(page.locator(".react-flow__node")).toHaveCount(3, { timeout: 20_000 })

  const run = page.getByRole("button", { name: "실행" })
  await expect(run).toBeEnabled({ timeout: 20_000 })
  await run.click()
  await page.getByLabel("item").fill("커피")
  await page.getByRole("button", { name: "실행", exact: true }).last().click()

  await expect(page.getByRole("dialog")).toBeVisible({ timeout: 30_000 })
  await expectRunStatus(page, /승인 대기/)

  await page.reload()

  // Same question, from the engine: the dialog is rebuilt out of the run's `waitingFor`, so its
  // message being right is evidence the answer came from the run rather than from a cached page.
  const dialog = page.getByRole("dialog")
  await expect(dialog).toBeVisible({ timeout: 30_000 })
  await expect(dialog).toContainText("이 주문을 승인해 주세요")
  await expect(dialog).toContainText("커피")
  await expectRunStatus(page, /승인 대기/)

  // The colours come back too. The approval node is parked; the start node before it has finished.
  await expect(
    page.locator(".react-flow__node", { hasText: "사람 승인" }).getByRole("img", { name: /실행 상태/ }),
  ).toHaveAccessibleName("실행 상태: 승인 대기")
  await expect(
    page.locator(".react-flow__node", { hasText: "시작" }).getByRole("img", { name: /실행 상태/ }),
  ).toHaveAccessibleName("실행 상태: 성공")

  // And it is still answerable, which a restored *picture* of a run would not be.
  await dialog.getByRole("button", { name: "승인" }).click()
  await expectRunStatus(page, /성공/)
})
