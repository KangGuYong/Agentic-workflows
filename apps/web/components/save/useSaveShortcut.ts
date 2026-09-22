"use client"

import { useEffect } from "react"

import { isSaveShortcut } from "@/lib/browser/shortcut"

/** Ctrl+S saves the document now, wherever the focus is.
 *
 * On the window, not the canvas: the person pressing it may be in a node's form, in a template field
 * or on a toolbar button, and "save" means the same thing from all of them.
 *
 * The keypress is consumed. Left alone it would open the browser's "save page as" dialog on top of
 * the editor -- an HTML file of the page, which is not the document anyone meant to save.
 */
export function useSaveShortcut(flush: () => Promise<void>): void {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (!isSaveShortcut(event)) return
      event.preventDefault()
      void flush()
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [flush])
}
