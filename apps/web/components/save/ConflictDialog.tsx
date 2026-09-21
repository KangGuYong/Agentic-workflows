"use client"

import { useEffect, useRef } from "react"

import type { SaveState } from "@/store/save"

/** The 409 dialog (3 설계 §9).
 *
 * Two buttons and no default. Both choices lose somebody's work, so neither is safe enough to happen by
 * itself -- which is also why nothing auto-retries behind this, and why the dialog cannot be dismissed
 * with Esc into a state where the person thinks saving resumed.
 */
export function ConflictDialog({ state }: { state: SaveState }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const open = state.conflict !== null

  useEffect(() => {
    const element = dialog.current
    if (element === null) return
    // `showModal` traps focus and Esc for us; doing it by hand is how a dialog ends up dismissable by a
    // stray click while a destructive choice is pending.
    if (open && !element.open) element.showModal()
    if (!open && element.open) element.close()
  }, [open])

  return (
    <dialog
      ref={dialog}
      aria-labelledby="conflict-title"
      onCancel={(event) => event.preventDefault()}
      // `m-auto` restores what centers a modal dialog: the UA stylesheet does it with `margin: auto`,
      // and Tailwind's preflight resets margins on every element, which leaves it jammed in the corner.
      className="m-auto max-w-sm border border-ink-600 bg-ink-800 p-4 text-fg backdrop:bg-black/60"
      style={{ borderRadius: "var(--radius)" }}
    >
      <h2 id="conflict-title" className="text-sm font-semibold">
        다른 곳에서 변경됨
      </h2>
      <p className="mt-2 text-xs text-fg-muted">
        이 워크플로를 다른 탭이나 다른 사람이 먼저 저장했습니다. 어느 쪽을 남길지 선택해 주세요. 선택하기
        전까지 자동 저장은 멈춰 있습니다.
      </p>

      <div className="mt-4 flex flex-col gap-2">
        <Choice
          title="저장된 내용 불러오기"
          cost="이 화면에서 한 편집은 사라집니다."
          onClick={state.reload}
        />
        <Choice
          title="내 변경으로 덮어쓰기"
          cost="다른 곳에서 저장한 내용이 사라집니다."
          onClick={() => void state.overwrite()}
        />
      </div>
    </dialog>
  )
}

/** Each button states what it costs. A choice between two unlabelled buttons is a coin toss. */
function Choice({ title, cost, onClick }: { title: string; cost: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="border border-ink-600 bg-ink-700 px-3 py-2 text-left text-xs hover:border-ink-500"
      style={{ borderRadius: "var(--radius)" }}
    >
      <span className="block">{title}</span>
      <span className="mt-0.5 block text-fg-faint">{cost}</span>
    </button>
  )
}
