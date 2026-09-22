/** Is this keypress the save shortcut?
 *
 * Ctrl+C -- and ⌘C, which is where a Mac keyboard puts the same chord. Both are also the browser's
 * copy, which the editor never takes away: the listener that acts on this does not `preventDefault`,
 * so text selected anywhere still copies and the document saves on top of that. Making the shortcut
 * *only* save would break copying inside every text field on the screen.
 *
 * Matched on `code` as well as `key`: with a Korean IME active, Chrome reports the C key as `ㅊ` in
 * `key`, and the shortcut must work on the keyboard this editor exists for.
 */
export function isSaveShortcut(event: {
  key: string
  code: string
  ctrlKey: boolean
  metaKey: boolean
  altKey: boolean
  repeat: boolean
}): boolean {
  if (!(event.ctrlKey || event.metaKey) || event.altKey || event.repeat) return false
  return event.key.toLowerCase() === "c" || event.code === "KeyC"
}
