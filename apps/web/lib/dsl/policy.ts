import type { Policy, RetrySpec } from "./document"

/** Reconciling the policy panel's form with what the DSL should store (3 설계 §5.4).
 *
 * The DSL carries *overrides*, not a copy of the node type's defaults. The panel has to show effective
 * values -- a blank timeout box next to "the engine will use 120s" helps nobody -- so it merges the
 * default in on the way out, and strips it back off on the way in. Both directions are pure and tested
 * because getting either wrong is invisible: the workflow still runs, it just gains a version on every
 * save, or silently freezes a default that was meant to follow the engine.
 */

function same(a: unknown, b: unknown): boolean {
  // Structural, not identity: these are small JSON values, and `defaultOutput` can be an object.
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null)
}

function retryOverride(form: RetrySpec | undefined, base: RetrySpec | undefined): RetrySpec | undefined {
  if (form === undefined) return undefined
  const kept: RetrySpec = {}
  for (const key of ["maxAttempts", "backoff", "initialDelaySec"] as const) {
    const value = form[key]
    if (value !== undefined && !same(value, base?.[key])) {
      Object.assign(kept, { [key]: value })
    }
  }
  return Object.keys(kept).length > 0 ? kept : undefined
}

/** What the document should store for this node: only what the form changed. */
export function policyOverride(form: Policy, base: Policy | null): Policy | undefined {
  const kept: Policy = {}
  for (const key of ["timeoutSec", "onError", "defaultOutput"] as const) {
    const value = form[key]
    if (value !== undefined && !same(value, base?.[key])) {
      Object.assign(kept, { [key]: value })
    }
  }
  const retry = retryOverride(form.retry, base?.retry)
  if (retry !== undefined) kept.retry = retry
  return Object.keys(kept).length > 0 ? kept : undefined
}

/** What the engine will actually use: the node's override laid over the node type's default. */
export function effectivePolicy(override: Policy | undefined, base: Policy | null): Policy {
  const merged: Policy = { ...(base ?? {}), ...(override ?? {}) }
  // Field by field, not object by object. A node overriding only `maxAttempts` would otherwise show an
  // empty backoff and delay in the panel, and saving the form would write those blanks into the DSL.
  if (base?.retry !== undefined || override?.retry !== undefined) {
    merged.retry = { ...(base?.retry ?? {}), ...(override?.retry ?? {}) }
  }
  return merged
}
