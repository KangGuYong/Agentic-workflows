import { expect, test } from "@playwright/test"

import {
  connect,
  createWorkflow,
  deleteWorkflow,
  deselect,
  editorPath,
  endNode,
  startNode,
} from "./support"

/** What the editor says about a document the engine would refuse (3 설계 §7, Task 21).
 *
 * The rule under test is the one that makes validation worth having: the 실행 button is disabled while
 * the document has errors, so a person is told before they press it rather than by a 422 afterwards.
 */

let workflowId: string

test.beforeEach(async ({ request }) => {
  // The template's `out` goes nowhere, which is HANDLE_NOT_CONNECTED -- an error, not a warning.
  const workflow = await createWorkflow(request, `e2e-validation-${Date.now()}`, {
    version: "1",
    nodes: [
      startNode({ name: { type: "string" } }),
      {
        id: "template_1",
        type: "template",
        position: { x: 320, y: 80 },
        config: { template: "안녕, {{ start.name }}!" },
      },
      endNode({ greeting: "{{ template_1.text }}" }, { x: 560, y: 80 }),
    ],
    edges: [{ id: "edge_1", source: "start", target: "template_1" }],
  })
  workflowId = workflow.id
})

test.afterEach(async ({ request }) => {
  await deleteWorkflow(request, workflowId)
})

test("an unconnected handle blocks the run until it is connected", async ({ page }) => {
  await page.goto(editorPath(workflowId))
  await expect(page.locator(".react-flow__node")).toHaveCount(3, { timeout: 20_000 })

  // The badge is on the node, in Korean, and names the worst issue rather than only its severity.
  const node = page.locator(".react-flow__node", { hasText: "템플릿" })
  const badge = node.getByRole("img", { name: /^오류/ })
  await expect(badge).toBeVisible({ timeout: 20_000 })
  await expect(badge).toHaveAccessibleName(/^오류: .+ 외 1건$/)

  // The badge has room for one line; the panel is where the rest of them are. This is the issue the
  // scenario is about, and the badge's own text is the *other* one.
  await node.click()
  await expect(page.getByRole("complementary", { name: "노드 설정" })).toContainText(
    "'out' 출력이 연결되지 않았습니다",
    { timeout: 10_000 },
  )

  const run = page.getByRole("button", { name: "실행" })
  await expect(run).toBeDisabled()
  // The count is the reason, and it is on the button for a screen reader, not only in a tooltip.
  await expect(run).toHaveAccessibleName(/오류 \d+건을 먼저 해결해 주세요/)

  await deselect(page)
  await connect(page, "template_1", "end", 2)

  // Re-validated against the engine, not re-decided locally: the button coming back is the editor
  // agreeing with the engine about a document it just changed.
  await expect(run).toBeEnabled({ timeout: 20_000 })
  await expect(badge).toHaveCount(0)
})
