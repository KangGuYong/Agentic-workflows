import { describe, expect, it } from "vitest"

import { candidates, typedAt, type RefNode } from "./complete"

const NODES: RefNode[] = [
  { id: "start", label: "시작", outputSchema: { type: "object", properties: { name: { type: "string" } } } },
  {
    id: "llm_1",
    label: "요약",
    outputSchema: {
      type: "object",
      properties: { text: { type: "string" }, meta: { type: "object", properties: { tokens: {} } } },
      required: ["text"],
    },
  },
  { id: "llm_2", label: "번역", outputSchema: { type: "object", properties: { text: { type: "string" } } } },
  { id: "end", label: "끝", outputSchema: {} },
]

/** What `/validate` reports as guaranteed for a node placed after `start` and `llm_1`. */
const GUARANTEED = ["start", "llm_1"]

function inserts(prefix: string, guaranteed: readonly string[] | null = GUARANTEED) {
  return candidates(prefix, guaranteed, NODES).map((candidate) => candidate.insert)
}

describe("completing a node reference", () => {
  it("offers every referenceable node after `{{ `", () => {
    expect(inserts(" ")).toEqual(["start", "llm_1", "llm_2"])
  })

  it("marks the ones the engine does not guarantee have run", () => {
    // MVP 9장: offer them, but say so. The engine reports the same thing as REF_NOT_GUARANTEED, and a
    // person who cannot see it here only finds out after the workflow refuses to save.
    const byId = new Map(candidates(" ", GUARANTEED, NODES).map((c) => [c.insert, c]))

    expect(byId.get("llm_1")?.guaranteed).toBe(true)
    expect(byId.get("llm_2")?.guaranteed).toBe(false)
    expect(byId.get("llm_2")?.detail).toContain("기본값")
  })

  it("never offers `end`, which the engine refuses to resolve", () => {
    // `engine/validator/refs.py::_check_ref` answers REF_UNKNOWN_NODE for `end`.
    expect(inserts(" ")).not.toContain("end")
  })

  it("inserts the node id but shows the node label", () => {
    // The person picks 요약; the document gets `llm_1`. Labels are not unique and are not identifiers.
    const summary = candidates(" ", GUARANTEED, NODES).find((c) => c.insert === "llm_1")

    expect(summary?.label).toBe("요약")
  })

  it("filters by what has been typed, against the id and the label alike", () => {
    expect(inserts(" llm_")).toEqual(["llm_1", "llm_2"])
    expect(inserts(" 번")).toEqual(["llm_2"])
  })

  it("says nothing about guarantees before validation has run", () => {
    // `null` is "not known yet", which is the editor's state until `/validate` answers. Marking every
    // node "기본값 필요" then would be a warning about nothing.
    const all = candidates(" ", null, NODES)

    expect(all.every((candidate) => candidate.guaranteed === null)).toBe(true)
    expect(all.some((candidate) => candidate.detail?.includes("기본값") === true)).toBe(false)
  })
})

describe("completing a field below a node", () => {
  it("offers the properties of the node's output schema", () => {
    expect(inserts(" llm_1.")).toEqual(["text", "meta"])
  })

  it("filters those by the partial field name", () => {
    expect(inserts(" llm_1.te")).toEqual(["text"])
  })

  it("goes deeper through a nested object", () => {
    expect(inserts(" llm_1.meta.")).toEqual(["tokens"])
  })

  it("offers nothing further once the path reaches a string", () => {
    expect(inserts(" llm_1.text.")).toEqual([])
  })

  it("offers nothing further when the typed path is already a complete scalar field", () => {
    // `{{ llm_1.text` is finished: `text` is a string, so there is nothing to add. A popup here would
    // re-offer the word already typed and swallow the next keypress.
    expect(inserts(" llm_1.text")).toEqual([])
  })

  it("still offers below a field that could go deeper", () => {
    // The control for the case above: `meta` is an object, so `{{ llm_1.meta` is not finished.
    expect(inserts(" llm_1.meta")).toEqual(["meta"])
  })

  it("offers nothing below a node it has no schema for", () => {
    const unknown: RefNode[] = [{ id: "llm_9", label: "새 노드", outputSchema: {} }]

    expect(candidates(" llm_9.", ["llm_9"], unknown)).toEqual([])
  })

  it("offers nothing below a node that does not exist", () => {
    expect(inserts(" nope.")).toEqual([])
  })

  it("marks a required property, so the list says which are always there", () => {
    const text = candidates(" llm_1.", GUARANTEED, NODES).find((c) => c.insert === "text")
    const meta = candidates(" llm_1.", GUARANTEED, NODES).find((c) => c.insert === "meta")

    expect(text?.detail).toContain("문자열")
    expect(meta?.detail).toContain("없을 수 있음")
  })
})

describe("where completion does not apply", () => {
  it("offers nothing after a filter, which is not a reference position", () => {
    expect(inserts(" llm_1.text | default(")).toEqual([])
    expect(inserts(" llm_1.text | ")).toEqual([])
  })

  it("offers nothing inside a string literal", () => {
    expect(inserts(' llm_1.text | default("')).toEqual([])
  })

  it("still offers after an operator, where a second reference may follow", () => {
    // The control for the two above: staying quiet everywhere would be easy and useless.
    expect(inserts(" llm_1.text + ")).toEqual(["start", "llm_1", "llm_2"])
  })
})

describe("typedAt", () => {
  it("is the token the cursor is in the middle of", () => {
    expect(typedAt(" llm_")).toBe("llm_")
    expect(typedAt(" llm_1.te")).toBe("te")
  })

  it("is empty right after the opening brace or a dot, where nothing is typed yet", () => {
    expect(typedAt(" ")).toBe("")
    expect(typedAt(" llm_1.")).toBe("")
  })

  it("is the Korean the person typed, so choosing a candidate replaces it", () => {
    // Appending `llm_1` to a stray `번` would leave `{{ 번llm_1 }}` in the document.
    expect(typedAt(" 번")).toBe("번")
  })

  it("is null where completion does not apply, matching `candidates`", () => {
    expect(typedAt(" a | ")).toBeNull()
    expect(typedAt(' "')).toBeNull()
  })
})
