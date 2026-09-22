"use client"

import { useEffect } from "react"

import { isSaveShortcut } from "@/lib/browser/shortcut"

/** Ctrl+C saves the document now, wherever the focus is.
 *
 * On the window, not the canvas: the person pressing it may be in a node's form, in a template field
 * or on a toolbar button, and "save" means the same thing from all of them. The keypress is not
 * consumed -- see `isSaveShortcut` -- so it is still the copy it always was.
 */
export function useSaveShortcut(flush: () => Promise<void>): void {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (isSaveShortcut(event)) void flush()
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [flush])
}
