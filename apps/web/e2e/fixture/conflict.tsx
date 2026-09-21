import { StrictMode, useState } from "react"
import { createRoot } from "react-dom/client"

import { ConflictDialog } from "@/components/save/ConflictDialog"
import { emptyDsl, type EditorDsl } from "@/lib/dsl/document"
import { createSaveStore, type SaveResult } from "@/store/save"

/** The conflict dialog in a real browser.
 *
 * jsdom has no `showModal`, so the unit tests shim it and can only assert that the element is open.
 * Modality -- the background not being clickable, Esc not dismissing a pending destructive choice --
 * only exists in a browser, and it is the part that matters here.
 */

const THEIRS: EditorDsl = {
  ...emptyDsl(),
  nodes: [{ id: "start", type: "start", position: { x: 9, y: 9 } }],
}

function Fixture() {
  const [clicks, setClicks] = useState(0)
  const [dsl, setDsl] = useState<EditorDsl>(emptyDsl())
  const [store] = useState(() =>
    createSaveStore({
      revision: 1,
      getDsl: () => dsl,
      onReload: setDsl,
      delayMs: 10,
      save: (): Promise<SaveResult> =>
        Promise.resolve({ outcome: "conflict", currentRevision: 5, draftDsl: THEIRS }),
    }),
  )
  const [state, setState] = useState(store.getState)
  useState(() => store.subscribe(setState))

  return (
    <div>
      <button type="button" data-testid="behind" onClick={() => setClicks((n) => n + 1)}>
        뒤에 있는 버튼
      </button>
      <button type="button" data-testid="conflict" onClick={state.changed}>
        충돌 일으키기
      </button>
      <pre data-testid="clicks">{clicks}</pre>
      <pre data-testid="nodes">{dsl.nodes.length}</pre>
      <ConflictDialog state={state} />
    </div>
  )
}

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <Fixture />
  </StrictMode>,
)
