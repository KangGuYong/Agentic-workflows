/** Telling a controlled editor's own edits apart from the parent's when they come back as `value`.
 *
 * A controlled editor reports every document through `onChange` and gets a `value` prop back. A parent
 * that commits synchronously hands back the very document the editor holds, and the two agree. A parent
 * that commits late -- batched, debounced, round-tripped through a store -- hands back an *earlier*
 * document while the editor has already moved on. Compared naively against the current document, that
 * stale echo reads as an external edit and gets written back over what was typed since. During a Korean
 * composition that costs the syllable; after one, it costs the commit (`e2e/fixture/template-ime.spec.ts`).
 *
 * The tracker remembers what the editor reported and has not yet seen come back. A `value` that is one
 * of those is an echo and must not be written back; one that is not is the parent's own change and
 * must be.
 */

/** A parent further behind than this is not lagging, it is ignoring `onChange`; forgetting the oldest
 * reports then bounds memory, and misreading one of them as external is the least wrong outcome. */
export const MAX_PENDING = 256

export interface Echoes {
  /** The editor just reported `doc` through `onChange`. */
  emitted(doc: string): void
  /** Whether `value` is a reported document coming back. Consumes it and every report before it, so a
   * parent that batched several reports into one render is caught up in one call. An external value
   * clears the pending reports: what the editor said before the parent overrode it no longer matters. */
  own(value: string): boolean
}

export function echoes(): Echoes {
  const pending: string[] = []
  return {
    emitted(doc) {
      pending.push(doc)
      if (pending.length > MAX_PENDING) pending.shift()
    },
    own(value) {
      const at = pending.indexOf(value)
      if (at === -1) {
        pending.length = 0
        return false
      }
      pending.splice(0, at + 1)
      return true
    },
  }
}
