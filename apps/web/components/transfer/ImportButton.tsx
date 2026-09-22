"use client"

import { useRef } from "react"

/** A file picker that looks like the buttons beside it (Task 23).
 *
 * A bare `<input type="file">` cannot be styled to match, so the input is hidden and a real button
 * opens it. Hidden with `sr-only` rather than `display: none`, so it keeps its accessible name and a
 * screen reader can still reach it directly.
 *
 * Shared by the list and the editor toolbar, which style their buttons differently -- hence
 * `className`. What must not differ between them is the input itself: the same accessible name means
 * one test and one habit cover both places.
 */
export function ImportButton({
  busy = false,
  className,
  style,
  title,
  label = "가져오기",
  multiple = false,
  accept = "application/json,.json",
  onPick,
}: {
  busy?: boolean
  className: string
  style?: React.CSSProperties
  title?: string
  /** Button text; defaults to the workflow-import wording so existing callers are unaffected. */
  label?: string
  /** When set, the input accepts several files and `onPick` fires once per file chosen. */
  multiple?: boolean
  /** The `accept` filter on the underlying input; defaults to the workflow-import (`.json`) filter.
   *  A caller picking other kinds of files -- documents, say -- passes its own, or `""` for none. */
  accept?: string
  onPick: (file: File) => void
}) {
  const input = useRef<HTMLInputElement>(null)

  return (
    <>
      <input
        ref={input}
        type="file"
        accept={accept}
        multiple={multiple}
        aria-label="워크플로 파일"
        className="sr-only"
        onChange={(event) => {
          const files = event.target.files
          // Cleared after every pick: choosing the *same* file twice in a row fires no `change` event
          // otherwise, which looks exactly like the import silently failing.
          event.target.value = ""
          if (files === null) return
          for (const file of Array.from(files)) onPick(file)
        }}
      />
      <button
        type="button"
        onClick={() => input.current?.click()}
        disabled={busy}
        title={title}
        className={className}
        style={style}
      >
        {label}
      </button>
    </>
  )
}
