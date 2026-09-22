import { expect, test } from "@playwright/test"

/** Korean input in the template editor (3 설계 §6.1, Task 10).
 *
 * This test needs a browser and cannot be moved into vitest. jsdom has no composition events, no
 * layout and no real selection, so the failure mode this guards against -- a decoration or a controlled
 * re-render landing in the middle of an IME composition and committing the syllable twice -- is
 * invisible there.
 *
 * It is the reason the editor never recreates its `EditorView` and never writes back an echo of a
 * document it reported itself (`lib/template/echo.ts`): with the echo check removed, the lagging
 * parent below fails "committed once" on every run. The editor also refuses to write back while
 * `view.composing` is true; that covers a parent changing the value *of its own accord* mid-composition,
 * which this fixture has no way to produce, so that guard is not what these tests exercise.
 */

const EDITOR = "[data-testid='template-editor'] .cm-content"
const VALUE = "[data-testid='value']"
const COMMITS = "[data-testid='commits']"

test.beforeEach(async ({ page }) => {
  await page.goto("/")
  await page.locator(EDITOR).waitFor()
})

test("Korean inserted directly appears once", async ({ page }) => {
  await page.locator(EDITOR).click()
  await page.keyboard.insertText("안녕하세요")

  await expect(page.locator(VALUE)).toHaveText("안녕하세요")
})

test("a composed syllable is committed once, not twice", async ({ page }) => {
  // The lagging parent: its `value` prop trails the document, so the editor's value effect has
  // something to write back. Written back while the IME is composing, it destroys the syllable; written
  // back just after the commit, it destroys the commit (the editor sees its own stale report "아", not
  // composing any more, and puts it over "안"). A parent that commits synchronously never reaches that
  // branch, which is why the first version of this test passed with the guard removed.
  await page.goto("/?lag=1")
  const editor = page.locator(EDITOR)
  await editor.waitFor()
  await editor.click()

  // Driven through CDP, which is the browser's own IME path rather than a hand-rolled event sequence.
  // Dispatching CompositionEvents and setting `textContent` was the first attempt and it tested the
  // wrong thing: writing the DOM directly destroys CodeMirror's selection, so the assertion failed on
  // the harness rather than on the editor.
  const cdp = await page.context().newCDPSession(page)
  // What a Korean IME sends: one composition, updated per jamo, then a single commit.
  for (const jamo of ["ㅇ", "아", "안"]) {
    await cdp.send("Input.imeSetComposition", {
      text: jamo,
      selectionStart: jamo.length,
      selectionEnd: jamo.length,
    })
  }
  await cdp.send("Input.insertText", { text: "안" })
  // Read the commit before typing the next syllable: a duplicated commit reads "안안" and a clobbered
  // one reads "아", and either fails on this line rather than the next, where it would be harder to
  // tell from a lost "녕".
  await expect(page.locator(VALUE)).toHaveText("안")
  await page.keyboard.insertText("녕")

  // Once, in order. A controlled wrapper that writes the document back mid-composition duplicates the
  // syllable here; one that recreates the view loses it.
  await expect(page.locator(VALUE)).toHaveText("안녕")
  // And the parent saw the syllable form, commit and grow exactly once each. A stale report written
  // back reaches the parent again as a fresh commit, so this line catches the write-back even when the
  // value happens to read right at the moment the lines above polled it. Without the editor's echo
  // check the log here runs "ㅇ,아,안,ㅇ,아,안,…" and never settles.
  await expect(page.locator(COMMITS)).toHaveText("ㅇ,아,안,안녕")
})

test("composing Korean next to a chip leaves both intact", async ({ page }) => {
  const editor = page.locator(EDITOR)
  await editor.click()
  await page.keyboard.insertText("{{ llm_1 }} ")
  // The cursor is now past the reference, so it closes into a chip. This is the state that makes the
  // test worth running: a decoration is live in the document while the IME opens a composition.
  await expect(page.locator(".cm-ref-chip")).toHaveText("요약")

  const cdp = await page.context().newCDPSession(page)
  for (const jamo of ["ㅇ", "요", "욕"]) {
    await cdp.send("Input.imeSetComposition", {
      text: jamo,
      selectionStart: jamo.length,
      selectionEnd: jamo.length,
    })
  }
  await cdp.send("Input.insertText", { text: "요약본" })

  // The chip is still one chip, and the composed text landed after it -- not inside it, not twice.
  await expect(page.locator(".cm-ref-chip")).toHaveCount(1)
  await expect(page.locator(VALUE)).toHaveText("{{ llm_1 }} 요약본")
})

test("completes a node by its Korean label and inserts its id", async ({ page }) => {
  const editor = page.locator(EDITOR)
  await editor.click()
  await page.keyboard.type("{{ 요")

  const option = page.locator(".cm-tooltip-autocomplete li", { hasText: "요약" })
  await expect(option).toBeVisible()
  await option.click()

  // The list showed 요약; the document gets `llm_1`. That is the whole point of the completion.
  await expect(page.locator(VALUE)).toHaveText("{{ llm_1")
})

test("a chip opens back into text when the cursor moves into it", async ({ page }) => {
  const editor = page.locator(EDITOR)
  await editor.click()
  await page.keyboard.insertText("{{ llm_1 }} 끝")
  await expect(page.locator(".cm-ref-chip")).toHaveText("요약")

  // A reference that cannot be edited cannot be corrected, and clicking into it is the first thing a
  // person does with a wrong one.
  await page.locator(".cm-ref-chip").click()
  await expect(page.locator(".cm-ref-chip")).toHaveCount(0)
  await expect(editor).toContainText("{{ llm_1 }}")
})
