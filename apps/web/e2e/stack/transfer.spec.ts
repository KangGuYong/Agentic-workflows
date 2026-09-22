import { readFileSync } from "node:fs"
import { join } from "node:path"

import { expect, test } from "@playwright/test"

import { createWorkflow, deleteWorkflow, editorPath, endNode, startNode } from "./support"

/** Taking a workflow out as a file and bringing one back in (Task 23).
 *
 * The round trip is the point: a file this editor wrote must open again as the same graph, and the
 * documents in `examples/` -- which nobody exported -- must open too.
 */

const EXAMPLE = join(process.cwd(), "..", "..", "examples", "03-parallel.json")

let importedId: string | null = null
let openId: string | null = null

test.afterEach(async ({ request }) => {
  if (importedId !== null) await deleteWorkflow(request, importedId)
  if (openId !== null) await deleteWorkflow(request, openId)
  importedId = null
  openId = null
})

test("an example file imports, opens, and exports back to the same document", async ({ page }) => {
  await page.goto("/")
  // The import control, not the 새 워크플로 button: a workflow *named* "새 워크플로" is a perfectly
  // ordinary row, and then that locator matches two things and the spec fails for nothing.
  await expect(page.locator("input[aria-label='워크플로 파일']")).toBeAttached({ timeout: 20_000 })

  // 1. Import creates a workflow and opens it. The file picker is hidden behind the 가져오기 button;
  //    `setInputFiles` drives the input directly, which is what a real pick ends up doing.
  await page.setInputFiles("input[aria-label='워크플로 파일']", EXAMPLE)
  await page.waitForURL(/\/workflows\/[0-9a-f-]+$/, { timeout: 20_000 })
  importedId = page.url().split("/").pop() ?? null
  expect(importedId).not.toBeNull()

  // 2. The graph is the file's graph, not an empty canvas.
  await expect(page.locator(".react-flow__node")).toHaveCount(5, { timeout: 20_000 })
  await expect(page.locator(".react-flow__node", { hasText: "모으기" })).toHaveCount(1)

  // 3. Export writes the document back out, named after the workflow.
  const [download] = await Promise.all([
    page.waitForEvent("download", { timeout: 20_000 }),
    page.getByRole("button", { name: "내보내기" }).click(),
  ])
  expect(download.suggestedFilename()).toBe("03-parallel.json")

  const path = await download.path()
  const exported = JSON.parse(readFileSync(path, "utf8")) as { nodes: { id: string }[]; edges: unknown[] }
  const original = JSON.parse(readFileSync(EXAMPLE, "utf8")) as { nodes: { id: string }[]; edges: unknown[] }

  // Node ids and edges survive the trip. Ids are what every template reference names, so a round trip
  // that changed one would quietly break every reference in the document.
  expect(exported.nodes.map((node) => node.id)).toEqual(original.nodes.map((node) => node.id))
  expect(exported.edges).toHaveLength(original.edges.length)
})

test("a file that is not a workflow is refused, and nothing is created", async ({ page }) => {
  await page.goto("/")
  // The import control, not the 새 워크플로 button: a workflow *named* "새 워크플로" is a perfectly
  // ordinary row, and then that locator matches two things and the spec fails for nothing.
  await expect(page.locator("input[aria-label='워크플로 파일']")).toBeAttached({ timeout: 20_000 })
  const before = await page.locator("tbody tr").count()

  await page.setInputFiles("input[aria-label='워크플로 파일']", {
    name: "junk.json",
    mimeType: "application/json",
    buffer: Buffer.from("{ not json }"),
  })

  // Not `getByRole("alert")`: Next renders `#__next-route-announcer__` with `role="alert"` on every
  // page, so that locator always matches two elements here and a single-value assertion on it fails
  // for a reason that has nothing to do with the import.
  await expect(page.locator("p[role='alert']")).toHaveText(/JSON 형식이 아닙니다/, { timeout: 10_000 })
  // Still on the list, and no workflow was left behind by a refused import.
  await expect(page).toHaveURL(/\/$/)
  await expect(page.locator("tbody tr")).toHaveCount(before)
})

test("importing in the editor places the file's nodes into the open workflow", async ({ page, request }) => {
  // The editor's 가져오기 is not the list's: it adds to the document on screen. The file's 시작/끝 are
  // left behind (the engine fixes their ids, so a document holds one of each), and 되돌리기 takes the
  // whole file back out in one press.
  const mine = await createWorkflow(request, `e2e-insert-${Date.now()}`, {
    version: "1",
    nodes: [startNode({ name: { type: "string" } }), endNode({ greeting: "{{ start.name }}" })],
    edges: [{ id: "edge_1", source: "start", target: "end" }],
  })
  openId = mine.id

  await page.goto(editorPath(openId))
  await expect(page.locator(".react-flow__node")).toHaveCount(2, { timeout: 20_000 })

  await page.setInputFiles("input[aria-label='워크플로 파일']", EXAMPLE)

  // Same workflow, more nodes: the example's three middle nodes, not its 시작/끝.
  await expect(page).toHaveURL(new RegExp(`/workflows/${openId}$`))
  await expect(page.locator(".react-flow__node")).toHaveCount(5, { timeout: 20_000 })
  await expect(page.locator(".react-flow__node", { hasText: "모으기" })).toHaveCount(1)
  await expect(page.locator(".react-flow__node", { hasText: "시작" })).toHaveCount(1)
  await expect(page.locator(".react-flow__node", { hasText: "끝" })).toHaveCount(1)

  // And it says what it did, including what it left behind.
  await expect(page.getByRole("status").filter({ hasText: "놓았습니다" })).toContainText("제외했습니다", {
    timeout: 10_000,
  })

  // One 되돌리기 takes the whole file back out.
  await page.getByRole("button", { name: "되돌리기" }).click()
  await expect(page.locator(".react-flow__node")).toHaveCount(2, { timeout: 10_000 })
})

test("importing twice renames the second copy instead of colliding", async ({ page, request }) => {
  const mine = await createWorkflow(request, `e2e-twice-${Date.now()}`, {
    version: "1",
    nodes: [startNode({ name: { type: "string" } }), endNode({ greeting: "{{ start.name }}" })],
    edges: [{ id: "edge_1", source: "start", target: "end" }],
  })
  openId = mine.id

  await page.goto(editorPath(openId))
  await expect(page.locator(".react-flow__node")).toHaveCount(2, { timeout: 20_000 })

  await page.setInputFiles("input[aria-label='워크플로 파일']", EXAMPLE)
  await expect(page.locator(".react-flow__node")).toHaveCount(5, { timeout: 20_000 })
  await page.setInputFiles("input[aria-label='워크플로 파일']", EXAMPLE)
  await expect(page.locator(".react-flow__node")).toHaveCount(8, { timeout: 20_000 })

  // Ids are what every reference names, so a collision would be a silently broken document.
  await expect(page.getByRole("status").filter({ hasText: "이름을 바꿨습니다" })).toBeVisible({ timeout: 10_000 })
})
