import { describe, expect, it } from "vitest"

import type { Policy } from "./document"
import { effectivePolicy, policyOverride } from "./policy"

/** What `/node-types` reports for an `llm` node. */
const DEFAULT: Policy = {
  timeoutSec: 120,
  retry: { maxAttempts: 3, backoff: "exponential", initialDelaySec: 2 },
  onError: "fail",
  defaultOutput: null,
}

describe("policyOverride", () => {
  it("keeps only what differs from the node type's default", () => {
    // The DSL carries overrides, not a copy of the defaults: writing a value the engine already uses
    // changes `dsl_hash` and creates a workflow version identical in behaviour to the one before it.
    const form: Policy = { ...DEFAULT, timeoutSec: 30 }

    expect(policyOverride(form, DEFAULT)).toEqual({ timeoutSec: 30 })
  })

  it("is undefined when the form matches the default exactly", () => {
    expect(policyOverride({ ...DEFAULT }, DEFAULT)).toBeUndefined()
  })

  it("is undefined for an empty form", () => {
    expect(policyOverride({}, DEFAULT)).toBeUndefined()
  })

  it("keeps only the retry fields that differ", () => {
    const form: Policy = { ...DEFAULT, retry: { ...DEFAULT.retry, maxAttempts: 1 } }

    expect(policyOverride(form, DEFAULT)).toEqual({ retry: { maxAttempts: 1 } })
  })

  it("drops `retry` entirely when every one of its fields matches", () => {
    const form: Policy = { ...DEFAULT, timeoutSec: 30, retry: { ...DEFAULT.retry } }

    expect(policyOverride(form, DEFAULT)).toEqual({ timeoutSec: 30 })
  })

  it("keeps a value the default does not mention at all", () => {
    const form: Policy = { timeoutSec: 30 }

    expect(policyOverride(form, null)).toEqual({ timeoutSec: 30 })
  })

  it("keeps a deliberate null that differs from the default", () => {
    // `defaultOutput: null` is the engine's default, but `onError: "default"` with a null output is a
    // real configuration -- the difference has to be judged per field, not by truthiness.
    const form: Policy = { ...DEFAULT, onError: "default", defaultOutput: { text: "" } }

    expect(policyOverride(form, DEFAULT)).toEqual({ onError: "default", defaultOutput: { text: "" } })
  })

  it("compares nested values structurally, not by identity", () => {
    // `defaultOutput` can be an object, and RJSF rebuilds `formData` on every keystroke, so the form's
    // copy is never the same reference as the node type's. An identity compare would call it changed
    // and write a "override" identical to the default -- a new workflow version per save, forever.
    const base: Policy = { ...DEFAULT, onError: "default", defaultOutput: { text: "" } }
    const form: Policy = { ...base, defaultOutput: { text: "" } }

    expect(form.defaultOutput).not.toBe(base.defaultOutput)
    expect(policyOverride(form, base)).toBeUndefined()
  })

  it("still sees a nested value the form actually changed", () => {
    // The control for the test above: structural comparison must not flatten every object to equal.
    const base: Policy = { ...DEFAULT, defaultOutput: { text: "" } }
    const form: Policy = { ...base, defaultOutput: { text: "비어 있음" } }

    expect(policyOverride(form, base)).toEqual({ defaultOutput: { text: "비어 있음" } })
  })
})

describe("effectivePolicy", () => {
  it("shows the default where the node overrides nothing", () => {
    expect(effectivePolicy(undefined, DEFAULT)).toEqual(DEFAULT)
  })

  it("lays the override over the default", () => {
    const merged = effectivePolicy({ timeoutSec: 30 }, DEFAULT)

    expect(merged.timeoutSec).toBe(30)
    expect(merged.retry).toEqual(DEFAULT.retry)
  })

  it("merges retry field by field rather than replacing the object", () => {
    // Otherwise opening the panel on a node that overrides only `maxAttempts` would show an empty
    // backoff and delay, and saving the form would write those blanks into the document.
    const merged = effectivePolicy({ retry: { maxAttempts: 1 } }, DEFAULT)

    expect(merged.retry).toEqual({ maxAttempts: 1, backoff: "exponential", initialDelaySec: 2 })
  })

  it("is the override alone for a node type with no default", () => {
    expect(effectivePolicy({ timeoutSec: 5 }, null)).toEqual({ timeoutSec: 5 })
  })

  it("is empty when there is neither", () => {
    expect(effectivePolicy(undefined, null)).toEqual({})
  })
})
