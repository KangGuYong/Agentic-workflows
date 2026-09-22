import { expect, type APIRequestContext, type Page } from "@playwright/test"

/** What every stack spec needs: a workflow of its own, and a way to be sure it is gone (3 설계 §11).
 *
 * Specs create through the editor's **own BFF proxy** rather than the engine directly. That needs no
 * token in the test process -- the proxy holds it -- and it exercises the same path the editor uses,
 * so a broken proxy fails the tests rather than being routed around by them.
 */

export interface Workflow {
  id: string
  revision: number
}

export async function createWorkflow(
  request: APIRequestContext,
  name: string,
  draftDsl: unknown,
): Promise<Workflow> {
  const created = await request.post("/api/engine/workflows", { data: { name } })
  expect(created.ok(), `workflow create failed: ${created.status()}`).toBe(true)
  // A fresh workflow is at revision **1**, not 0: `create` writes a row and that write is a revision.
  // Assuming 0 here produced a 409 on the very first save.
  const { id, revision: created_revision } = (await created.json()) as { id: string; revision: number }

  const saved = await request.put(`/api/engine/workflows/${id}`, {
    data: { draftDsl, revision: created_revision },
  })
  expect(saved.ok(), `draft save failed: ${saved.status()}`).toBe(true)
  const { revision } = (await saved.json()) as { revision: number }

  return { id, revision }
}

/** Delete, tolerating a workflow that a test already removed. Called from `afterEach`, where throwing
 * would hide the failure that mattered. */
export async function deleteWorkflow(request: APIRequestContext, id: string): Promise<void> {
  await request.delete(`/api/engine/workflows/${id}`).catch(() => undefined)
}

/** The engine refuses to delete a workflow with an active run, so a parked one is cancelled first. */
export async function cancelRuns(request: APIRequestContext, workflowId: string): Promise<void> {
  const listed = await request.get(`/api/engine/workflows/${workflowId}/runs`).catch(() => null)
  if (listed === null || !listed.ok()) return
  const { runs } = (await listed.json()) as { runs?: { id: string; status: string }[] }
  for (const run of runs ?? []) {
    if (run.status === "queued" || run.status === "running" || run.status === "waiting") {
      await request.post(`/api/engine/runs/${run.id}/cancel`).catch(() => undefined)
    }
  }
}

export function editorPath(workflowId: string): string {
  return `/workflows/${workflowId}`
}

/** A start node whose declared inputs are exactly `properties`. */
export function startNode(properties: Record<string, unknown>, position = { x: 80, y: 80 }) {
  return {
    id: "start",
    type: "start",
    position,
    config: { inputs: { type: "object", properties, required: Object.keys(properties) } },
  }
}

export function endNode(outputs: Record<string, string>, position = { x: 620, y: 80 }) {
  return { id: "end", type: "end", position, config: { outputs } }
}

/** Wait for the run status bar to read one of `texts`.
 *
 * Every wait in these specs is bounded and expressed as an assertion on something the UI shows. There
 * is no `waitForTimeout` anywhere: sleeping for a fixed time is not synchronisation, it is a guess
 * that fails on a slow machine and wastes time on a fast one.
 */
export async function expectRunStatus(page: Page, pattern: RegExp, timeout = 30_000): Promise<void> {
  await expect(page.getByRole("status", { name: "실행 상태" })).toHaveText(pattern, { timeout })
}

export async function expectSaved(page: Page, timeout = 15_000): Promise<void> {
  await expect(page.getByRole("status", { name: "저장 상태" })).toHaveText(/저장됨/, { timeout })
}

/** Press the save shortcut once there is something to save.
 *
 * Autosave runs once a minute, which is longer than any wait a spec should have, so a spec that needs
 * the document on the engine saves the way a person in a hurry does. Waiting for 저장 대기 중 first:
 * a press before the edit has reached the save slice would find nothing to save and do nothing.
 */
export async function pressSave(page: Page): Promise<void> {
  await expect(page.getByRole("status", { name: "저장 상태" })).toHaveText(/저장 대기 중/, { timeout: 10_000 })
  await page.keyboard.press("Control+c")
}

export async function saveNow(page: Page): Promise<void> {
  await pressSave(page)
  await expectSaved(page)
}

/** Drag a palette entry onto the canvas and wait for the node to exist.
 *
 * `targetPosition` is relative to the canvas, and the canvas is not the window: the node panel takes
 * 320px off its right edge whenever a node is selected, so a position chosen against the window would
 * land somewhere else depending on what happened to be selected.
 */
export async function dropNode(page: Page, label: string, at: { x: number; y: number }): Promise<void> {
  const before = await page.locator(".react-flow__node").count()
  await page.locator("[draggable='true']", { hasText: label }).first()
    .dragTo(page.locator(".canvas-grid"), { targetPosition: at })
  await expect(page.locator(".react-flow__node")).toHaveCount(before + 1, { timeout: 10_000 })
}

/** Click empty canvas to clear the selection, and wait for the node panel to actually be gone.
 *
 * Not cosmetic. The panel is 320px of the window, so while it is open the nodes on the right of the
 * graph are behind it -- present in the DOM, reported visible, and not reachable by a mouse. A drag
 * aimed at one of them silently does nothing, which is exactly the failure this helper exists to
 * prevent.
 */
export async function deselect(page: Page): Promise<void> {
  const canvas = await page.locator(".canvas-grid").boundingBox()
  if (canvas === null) throw new Error("canvas not on screen")
  // Raw mouse rather than `locator.click`: the pane under the pointer is mid-animation after a drop,
  // and Playwright's actionability check waits for it to settle in a place it never settles.
  await page.mouse.click(canvas.x + 40, canvas.y + 40)
  await expect(page.getByRole("complementary", { name: "노드 설정" })).toHaveCount(0, { timeout: 5_000 })
}

/** Drag from one node's source handle to another's target handle.
 *
 * React Flow exposes no API a test can call, so this is a real mouse drag, and everything that makes a
 * real drag fail applies: the handles are 11px, the connection target is picked up from pointer
 * *movement* over it, and either end can be hidden behind a panel.
 */
export async function connect(page: Page, from: string, to: string, expectedEdges: number): Promise<void> {
  // `data-nodeid` sits on the handle itself, which is the only attribute naming both ends of the drag
  // unambiguously -- the node wrapper carries `data-id` but says nothing about which handle.
  const source = page.locator(`.react-flow__handle.source[data-nodeid="${from}"]`).first()
  const target = page.locator(`.react-flow__handle.target[data-nodeid="${to}"]`).first()
  await expect(source).toBeVisible({ timeout: 10_000 })
  await expect(target).toBeVisible({ timeout: 10_000 })

  // Read immediately before the drag: a previous connection re-rendered the canvas, and a box captured
  // before that is a box of where the handle used to be.
  const start = await source.boundingBox()
  const end = await target.boundingBox()
  if (start === null || end === null) throw new Error(`handle not on screen: ${from} -> ${to}`)

  await expectHittable(page, start, `${from} source handle`)
  await expectHittable(page, end, `${to} target handle`)

  await page.mouse.move(start.x + start.width / 2, start.y + start.height / 2)
  await page.mouse.down()
  // Stepped, and away from the source first: React Flow starts the connection on the first move and
  // finds its target from movement over it, so a single jump to the destination can be missed.
  await page.mouse.move(start.x + start.width / 2 + 24, start.y + start.height / 2, { steps: 4 })
  await page.mouse.move(end.x + end.width / 2, end.y + end.height / 2, { steps: 16 })
  await page.mouse.up()
  // Off the handle, so the next drag starts from a clean hover state.
  await page.mouse.move(10, 10)

  // Per drag rather than once at the end: with two connections made and one edge drawn, a total count
  // does not say which of the two drags failed.
  await expect(
    page.locator(".react-flow__edge"),
    `connecting ${from} -> ${to} produced no edge`,
  ).toHaveCount(expectedEdges, { timeout: 10_000 })
}

/** Fail with the name of whatever covers `box` rather than with "no edge appeared".
 *
 * `toBeVisible` asks whether an element has a box and is not `display:none`; it does not ask whether a
 * mouse can reach it. A handle behind the node panel passes that check and swallows every click.
 */
async function expectHittable(page: Page, box: { x: number; y: number; width: number; height: number }, what: string) {
  const covering = await page.evaluate(
    ({ x, y }) => {
      const el = document.elementFromPoint(x, y)
      if (el === null) return "nothing (off screen)"
      if (el.closest(".react-flow__handle") !== null) return null
      return `${el.tagName.toLowerCase()}.${el.className.toString().split(" ").slice(0, 3).join(".")}`
    },
    { x: box.x + box.width / 2, y: box.y + box.height / 2 },
  )
  expect(covering, `${what} is covered by ${covering ?? ""}; a drag to it would do nothing`).toBeNull()
}

/** A start → 사람 승인 → 끝 graph, used by the approval and reload scenarios.
 *
 * Both of the approval node's handles are wired: an unconnected one is a validation **error**, which
 * would disable the run button and make these specs fail for a reason neither is about.
 */
export function approvalWorkflow() {
  return {
    version: "1",
    nodes: [
      startNode({ item: { type: "string" } }),
      {
        id: "approval_1",
        type: "human_approval",
        position: { x: 340, y: 80 },
        config: { message: "이 주문을 승인해 주세요.", review: "{{ start.item }}" },
      },
      endNode({ decision: "{{ approval_1.decision }}" }),
    ],
    edges: [
      { id: "edge_1", source: "start", target: "approval_1" },
      { id: "edge_2", source: "approval_1", sourceHandle: "approve", target: "end" },
      { id: "edge_3", source: "approval_1", sourceHandle: "reject", target: "end" },
    ],
  }
}
