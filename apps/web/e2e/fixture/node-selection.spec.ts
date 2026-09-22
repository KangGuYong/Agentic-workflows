import { expect, test, type Page } from "@playwright/test"

/** The node panel follows every click, not only the first (canvas fixture).
 *
 * React Flow is controlled here, and rebuilds what it knows about each node from the objects it is
 * given whenever the graph is re-derived -- on any edit, any validation response, any run event. If
 * those objects do not carry the selection, React Flow forgets it at that moment while the store still
 * holds it, and its next click sends no "deselect the previous one": the store ends up with two nodes
 * selected, and a panel that opens on exactly one does not open. The selection the store makes itself
 * (a dropped node is selected so its panel opens) was never known to React Flow at all, so the same
 * thing happened after every drop. In use it looked like the panel opening at random.
 */

const panel = (page: Page) => page.getByRole("complementary", { name: "노드 설정" })
const node = (page: Page, id: string) => page.locator(`.react-flow__node[data-id="${id}"]`)

test.beforeEach(async ({ page }) => {
  await page.goto("/canvas.html")
  await expect(page.locator(".react-flow__node")).toHaveCount(3)
})

test("clicking another node after an edit switches the panel to it", async ({ page }) => {
  await node(page, "llm_1").click()
  await expect(panel(page)).toContainText("llm_1")

  // An edit re-derives the graph, which is when React Flow used to lose the selection.
  await page.getByRole("tab", { name: "라벨" }).click()
  await page.getByLabel("라벨").fill("요약")
  await expect(node(page, "llm_1")).toContainText("요약")

  await node(page, "end").click()
  await expect(panel(page)).toContainText("end")
  await expect(panel(page)).not.toContainText("llm_1")
})

test("clicking another node after a drop switches the panel to it", async ({ page }) => {
  const before = await page.locator(".react-flow__node").count()
  await page.locator("[draggable='true']", { hasText: "LLM" }).first()
    .dragTo(page.locator(".canvas-grid"), { targetPosition: { x: 320, y: 320 } })
  await expect(page.locator(".react-flow__node")).toHaveCount(before + 1)
  // The drop selects what it placed, so the panel is already open on the new node.
  await expect(panel(page)).toContainText("llm_2")

  await node(page, "start").click()
  await expect(panel(page)).toContainText("start")
  await expect(panel(page)).not.toContainText("llm_2")
})

test("clicking empty canvas after an edit closes the panel", async ({ page }) => {
  await node(page, "llm_1").click()
  await page.getByRole("tab", { name: "라벨" }).click()
  await page.getByLabel("라벨").fill("요약")
  await expect(node(page, "llm_1")).toContainText("요약")

  const canvas = await page.locator(".canvas-grid").boundingBox()
  if (canvas === null) throw new Error("canvas not on screen")
  await page.mouse.click(canvas.x + canvas.width - 40, canvas.y + canvas.height - 40)
  await expect(panel(page)).toHaveCount(0)
})
