/** Is this keypress the save shortcut?
 *
 * Ctrl+S -- and ⌘S, which is where a Mac keyboard puts the same chord. The browser's own Ctrl+S saves
 * the *page* to a file, which is never what someone editing a workflow means, so the listener that
 * acts on this consumes the keypress (`useSaveShortcut`) rather than letting both happen.
 *
 * Either half matches, because neither alone covers both keyboards this has to work on. `code` is the
 * physical key, which is what a Korean IME leaves recognisable -- with one active, Chrome reports the
 * S key as `ㄴ` in `key`. `key` is the character, which is where a layout that moves S (Dvorak) puts
 * save, and where the browser's own Ctrl+S is.
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
  return event.key.toLowerCase() === "s" || event.code === "KeyS"
}
