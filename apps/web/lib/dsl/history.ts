import type { EditorDsl } from "./document"

/** Undo/redo over whole-document snapshots (3 설계 §5.1).
 *
 * Snapshots, not inverse operations. An inverse for every command is more code and, more to the point,
 * more code that can be *wrong* in a way nothing notices until a user undoes something unusual. The DSL
 * is capped at 512 KB and the stack at 50 entries, so the worst case is bounded and the realistic case
 * is far smaller. Keeping snapshots is only safe because every command in `commands.ts` is pure: a
 * command that mutated a node in place would reach back and change the snapshots too.
 *
 * `past` holds the states *before* each applied command, oldest first. `present` is what the editor
 * shows. `future` holds what undo walked back past, nearest first.
 */

/** Deep enough for a real editing session; 50 * 512 KB is the bound nobody will reach. */
export const MAX_HISTORY = 50

export interface History {
  present: EditorDsl
  past: EditorDsl[]
  future: EditorDsl[]
  /** The open merge window, if a drag is in progress. */
  mergeKey: string | null
}

export interface ApplyOptions {
  /**
   * Collapses consecutive applies that share a key into one undo step -- a drag emits a position
   * update per frame and none of them is a step a user wants to undo through.
   */
  mergeKey?: string
}

export function createHistory(present: EditorDsl): History {
  return { present, past: [], future: [], mergeKey: null }
}

export function canUndo(history: History): boolean {
  return history.past.length > 0
}

export function canRedo(history: History): boolean {
  return history.future.length > 0
}

export function apply(history: History, next: EditorDsl, options: ApplyOptions = {}): History {
  // Commands return their input by reference when they cannot do anything (a duplicate edge, a node
  // that is not there). Recording that would spend an undo step on nothing happening.
  if (next === history.present) return history

  const mergeKey = options.mergeKey ?? null
  const merging = mergeKey !== null && mergeKey === history.mergeKey
  // The direction that is easy to get backwards: a merged apply keeps the snapshot already on the
  // stack, because *that* is the state undo has to return to. Replacing it with the current frame would
  // make undo land wherever the drag last passed through instead of where it started.
  const past = merging ? history.past : [...history.past, history.present].slice(-MAX_HISTORY)

  return { present: next, past, future: [], mergeKey }
}

export function undo(history: History): History {
  const previous = history.past.at(-1)
  if (previous === undefined) return history
  return {
    present: previous,
    past: history.past.slice(0, -1),
    future: [history.present, ...history.future],
    // Ending the window matters: a move right after an undo would otherwise merge into the step the
    // undo just returned to, and the two would collapse together.
    mergeKey: null,
  }
}

export function redo(history: History): History {
  const [next, ...rest] = history.future
  if (next === undefined) return history
  return {
    present: next,
    past: [...history.past, history.present].slice(-MAX_HISTORY),
    future: rest,
    mergeKey: null,
  }
}

/** Ends the open merge window. The canvas calls this when a drag finishes. */
export function commit(history: History): History {
  return history.mergeKey === null ? history : { ...history, mergeKey: null }
}
