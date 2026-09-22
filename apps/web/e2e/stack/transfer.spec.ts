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

test("importing in the editor replaces the document, edges and all", async ({ page, request }) => {
  // The editor's 가져오기 opens the file **in place of** what is on screen: its 시작/끝 nodes and its
  // edges come too, so the graph arrives already wired rather than as loose nodes to connect by hand.
  const mine = await createWorkflow(request, `e2e-load-${Date.now()}`, {
    version: "1",
    nodes: [startNode({ name: { type: "string" } }), endNode({ greeting: "{{ start.name }}" })],
    edges: [{ id: "edge_1", source: "start", target: "end" }],
  })
  openId = mine.id

  await page.goto(editorPath(openId))
  await expect(page.locator(".react-flow__node")).toHaveCount(2, { timeout: 20_000 })
  await expect(page.locator(".react-flow__edge")).toHaveCount(1)

  await page.setInputFiles("input[aria-label='워크플로 파일']", EXAMPLE)

  // Same workflow, now holding the example's whole graph: five nodes and its five edges.
  await expect(page).toHaveURL(new RegExp(`/workflows/${openId}$`))
  await expect(page.locator(".react-flow__node")).toHaveCount(5, { timeout: 20_000 })
  await expect(page.locator(".react-flow__edge")).toHaveCount(5)
  await expect(page.locator(".react-flow__node", { hasText: "모으기" })).toHaveCount(1)
  // One start and one end -- the file's, not the old document's kept alongside them. By id, not by
  // label: the engine fixes these two ids, and a file is free to label them anything (this one calls
  // its start 배포 정보).
  await expect(page.locator('.react-flow__node[data-id="start"]')).toHaveCount(1)
  await expect(page.locator('.react-flow__node[data-id="end"]')).toHaveCount(1)

  // Wired means valid: the run button is not blocked by unconnected handles.
  await expect(page.getByRole("button", { name: "실행" })).toBeEnabled({ timeout: 20_000 })

  await expect(page.getByRole("status").filter({ hasText: "불러왔습니다" })).toContainText("연결", {
    timeout: 10_000,
  })

  // And one 되돌리기 brings the previous document back, which is what makes a mis-picked file survivable.
  await page.getByRole("button", { name: "되돌리기" }).click()
  await expect(page.locator(".react-flow__node")).toHaveCount(2, { timeout: 10_000 })
  await expect(page.locator(".react-flow__edge")).toHaveCount(1)
})
