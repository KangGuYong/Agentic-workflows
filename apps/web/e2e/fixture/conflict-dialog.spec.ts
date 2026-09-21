import { expect, test } from "@playwright/test"

/** What jsdom cannot answer about the conflict dialog (3 설계 §9).
 *
 * The unit tests shim `showModal` and assert the element is open. Modality is a browser behaviour, and
 * it is the one that matters here: while the dialog holds a choice that loses somebody's work either
 * way, nothing behind it may be clicked and no keypress may dismiss it into a state where the person
 * believes saving resumed.
 */

test.beforeEach(async ({ page }) => {
  await page.goto("/conflict.html")
  await page.getByTestId("conflict").click()
  await expect(page.getByRole("dialog")).toBeVisible()
})

test("the page behind it cannot be clicked", async ({ page }) => {
  // A click lands on the dialog's backdrop, not on the button underneath it.
  await page.getByTestId("behind").click({ force: true, position: { x: 2, y: 2 } })

  await expect(page.getByTestId("clicks")).toHaveText("0")
})

test("Esc does not dismiss the choice", async ({ page }) => {
  await page.keyboard.press("Escape")

  await expect(page.getByRole("dialog")).toBeVisible()
})

test("the document is not replaced until 불러오기 is chosen", async ({ page }) => {
  await expect(page.getByTestId("nodes")).toHaveText("0")

  await page.getByRole("button", { name: /저장된 내용 불러오기/ }).click()

  await expect(page.getByTestId("nodes")).toHaveText("1")
  await expect(page.getByRole("dialog")).toBeHidden()
})

test("focus is inside the dialog, not on the page behind it", async ({ page }) => {
  const focused = await page.evaluate(() => document.activeElement?.closest("dialog") !== null)

  expect(focused).toBe(true)
})

test("sits in the middle of the viewport, not in a corner", async ({ page }) => {
  // The UA stylesheet centres a modal dialog with `margin: auto`, and Tailwind's preflight resets
  // margins on every element -- which left it in the top-left corner looking like a rendering bug.
  const box = await page.getByRole("dialog").boundingBox()
  const viewport = page.viewportSize()
  if (box === null || viewport === null) throw new Error("no dialog on screen")

  const centreX = box.x + box.width / 2
  expect(Math.abs(centreX - viewport.width / 2)).toBeLessThan(40)
})
