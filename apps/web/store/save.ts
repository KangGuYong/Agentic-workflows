import { createStore, type StoreApi } from "zustand/vanilla"

import type { EditorDsl } from "@/lib/dsl/document"

/** Autosave and revision conflicts (3 설계 §9).
 *
 * The editor saves a **draft**, not a version: the engine accepts a draft that does not validate,
 * because half-built work is the normal state of an editor. Every save carries the `revision` it was
 * built on, and the engine's compare-and-set answers 409 when someone else got there first.
 *
 * Saving is **once a minute, or on demand**. The minute starts at the first unsaved change and is not
 * restarted by the changes after it -- a debounce would let someone who never pauses go an hour
 * without a save. `flush` is the on-demand path: the Ctrl+C shortcut, and 실행, which must run what
 * is on screen rather than what the engine last heard.
 *
 * The one rule that shapes the rest: **the document is never replaced unless the person chose it.**
 * Taking the other tab's draft on a conflict, silently, would throw away work that is only in this tab.
 * So a 409 stops autosaving and asks, and nothing here retries on its own -- two tabs auto-retrying at
 * each other is a race where whoever types last wins and the other's work disappears.
 */

export const SAVE_DELAY_MS = 60_000

export type SaveResult =
  | { outcome: "saved"; revision: number }
  | { outcome: "conflict"; currentRevision: number; draftDsl: EditorDsl }
  | { outcome: "failed"; message: string }

export type SaveRequest = (body: { draftDsl: EditorDsl; revision: number }) => Promise<SaveResult>

export type SaveStatus = "saved" | "pending" | "saving" | "conflict" | "error"

export interface Conflict {
  currentRevision: number
  draftDsl: EditorDsl
}

export interface SaveState {
  revision: number
  status: SaveStatus
  /** When the last successful save landed, for the status bar. */
  savedAt: number | null
  /** Why the last save failed, in the engine's own words. */
  error: string | null
  /** The other side's draft, while the dialog is open. Never applied without a choice. */
  conflict: Conflict | null

  /** The document changed: schedule a save, unless one is already scheduled. */
  changed: () => void
  /** Save now, skipping the wait. Resolves once nothing unsaved is left in the air -- or, on a
   * failure or a conflict, once that is what the state says. A no-op when there is nothing to save. */
  flush: () => Promise<void>
  /** Take the other side's draft, adopting its revision. */
  reload: () => void
  /** Resend this document against the revision that won. */
  overwrite: () => Promise<void>
  /** Leave the conflict open but stop showing the dialog. */
  dismissConflict: () => void
}

export type SaveStore = StoreApi<SaveState>

export interface SaveOptions {
  revision: number
  /** The document to save, read at save time rather than passed in: a save that is one debounce old
   * must send what the document is *now*, not what it was when the timer started. */
  getDsl: () => EditorDsl
  save: SaveRequest
  /** Replace the editor's document. Called only from `reload`, and only by a person's choice. */
  onReload: (dsl: EditorDsl) => void
  delayMs?: number
}

export function createSaveStore(options: SaveOptions): SaveStore {
  const delayMs = options.delayMs ?? SAVE_DELAY_MS

  return createStore<SaveState>((set, get) => {
    let timer: ReturnType<typeof setTimeout> | null = null
    /** The save in the air, follow-ups included, so `flush` can wait for it rather than race it. */
    let inFlight: Promise<void> | null = null
    /** A change arrived while a save was in the air. Exactly one follow-up save, not one per change. */
    let again = false

    function cancelTimer() {
      if (timer !== null) {
        clearTimeout(timer)
        timer = null
      }
    }

    /** One save, and the one follow-up it may need. `inFlight` is the whole chain. */
    function start(revision: number): Promise<void> {
      inFlight = send(revision).finally(() => {
        inFlight = null
      })
      return inFlight
    }

    async function send(revision: number): Promise<void> {
      set({ status: "saving" })
      let result: SaveResult
      try {
        result = await options.save({ draftDsl: options.getDsl(), revision })
      } catch (error) {
        // A thrown request is a failed one. The document is untouched either way; the difference is
        // only in what the status bar can say.
        result = { outcome: "failed", message: error instanceof Error ? error.message : "저장하지 못했습니다" }
      }

      if (result.outcome === "saved") {
        // Plainly "saved": when a follow-up is queued, the `send` below sets "saving" synchronously
        // before anything can render, so a "pending" here would be a state no one ever sees.
        set({ revision: result.revision, status: "saved", savedAt: Date.now(), error: null, conflict: null })
      } else if (result.outcome === "conflict") {
        // Stop. Not a retry, not a merge: the two drafts are both someone's work and only a person can
        // say which one survives.
        again = false
        cancelTimer()
        set({ status: "conflict", conflict: { currentRevision: result.currentRevision, draftDsl: result.draftDsl } })
        return
      } else {
        set({ status: "error", error: result.message })
        // Deliberately no retry timer. The next change retries; a failing engine does not need this
        // editor knocking every second, and the work is safe in the browser meanwhile.
        again = false
        return
      }

      if (again) {
        again = false
        await send(get().revision)
      }
    }

    return {
      revision: options.revision,
      status: "saved",
      savedAt: null,
      error: null,
      conflict: null,

      changed() {
        // A conflict is unresolved until the person resolves it. Saving over it is the thing this
        // whole slice exists to prevent.
        if (get().status === "conflict") return
        if (inFlight !== null) {
          again = true
          return
        }
        // A timer already running keeps its due time. Restarting it here would be a debounce, and a
        // debounce saves nothing for as long as someone keeps editing.
        if (timer !== null) return
        set({ status: "pending" })
        timer = setTimeout(() => {
          timer = null
          void start(get().revision)
        }, delayMs)
      },

      async flush() {
        if (get().status === "conflict") return
        // Whatever changed during that save is already queued behind it as `again`, so waiting for the
        // chain is waiting for the document as it is now.
        if (inFlight !== null) {
          await inFlight
          return
        }
        // Nothing unsaved: sending the same draft again would bump the revision for no reason, and
        // another tab holding the old one would then be told it conflicts. "error" is unsaved work too,
        // and the person's press is the retry.
        const { status } = get()
        if (status !== "pending" && status !== "error") return
        cancelTimer()
        await start(get().revision)
      },

      reload() {
        const conflict = get().conflict
        if (conflict === null) return
        options.onReload(conflict.draftDsl)
        set({ revision: conflict.currentRevision, status: "saved", savedAt: Date.now(), conflict: null, error: null })
      },

      async overwrite() {
        const conflict = get().conflict
        if (conflict === null) return
        // The person's click *is* the one retry (3 설계 §9). A second 409 comes back here and asks
        // again rather than looping -- a loop is two tabs overwriting each other forever.
        set({ revision: conflict.currentRevision, conflict: null, status: "saving" })
        await start(conflict.currentRevision)
      },

      dismissConflict() {
        set({ conflict: null, status: "error", error: "다른 곳에서 먼저 저장했습니다. 저장이 멈춰 있습니다." })
      },
    }
  })
}
