/** Hand the browser a file to save.
 *
 * A `Blob` and an object URL rather than a `data:` URI: a data URI puts the whole document in the
 * href, which browsers cap and which shows up in history. The URL is revoked on the next task -- not
 * immediately, because the click is dispatched synchronously but the download starts after it, and
 * revoking too early cancels it.
 *
 * Lives outside `lib/dsl/` on purpose: what a workflow file contains is a document question and is
 * tested on node; how a browser saves one is not.
 */
export function downloadText(fileName: string, text: string, type = "application/json"): void {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const link = document.createElement("a")
  link.href = url
  link.download = fileName
  // Appended before clicking: Firefox ignores a click on an anchor that is not in the document.
  document.body.append(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
