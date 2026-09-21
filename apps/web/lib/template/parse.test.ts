import { describe, expect, it } from "vitest"

import { openReferenceAt, ranges } from "./parse"

describe("ranges", () => {
  it("finds a reference with its offsets and inner text", () => {
    const text = "a {{ b.c }} d"

    expect(ranges(text)).toEqual([{ start: 2, end: 11, inner: "b.c" }])
    expect(text.slice(2, 11)).toBe("{{ b.c }}")
  })

  it("finds several, in document order", () => {
    expect(ranges("{{ a }}{{ b }}").map((range) => range.inner)).toEqual(["a", "b"])
  })

  it("gives no range for an unclosed `{{`", () => {
    // This is the state the document is in for every keystroke between typing `{{` and closing it. A
    // chip over an unclosed range would swallow the rest of the field while the person types.
    expect(ranges("a {{ b.c")).toEqual([])
    expect(ranges("{{ a }} then {{ b")).toHaveLength(1)
  })

  it("ignores `{{` inside a Jinja comment", () => {
    expect(ranges("{# {{ a }} #}")).toEqual([])
  })

  it("ignores `{{` inside a statement block", () => {
    expect(ranges("{% if x %}")).toEqual([])
    expect(ranges("{% for i in xs %}{{ i }}{% endfor %}").map((range) => range.inner)).toEqual(["i"])
  })

  it("stops at an unclosed comment or block rather than reading past it", () => {
    // Jinja would reject the template; until it is closed, nothing after it is a reference yet.
    expect(ranges("{# open {{ a }}")).toEqual([])
    expect(ranges("{% open {{ a }}")).toEqual([])
  })

  it("does not end a reference on a `}}` inside a string literal", () => {
    // `{{ a | default("}}") }}` is a real template. Ending the range at the first `}}` would put the
    // chip over half of it and leave a dangling `") }}` in the text.
    const text = `{{ a | default("}}") }}`

    expect(ranges(text)).toEqual([{ start: 0, end: text.length, inner: `a | default("}}")` }])
  })

  it("strips the whitespace-control dashes from the inner text", () => {
    expect(ranges("{{- a.b -}}")[0]?.inner).toBe("a.b")
  })

  it("keeps an empty reference, which is what `{{ }}` is while being typed", () => {
    expect(ranges("{{ }}")).toEqual([{ start: 0, end: 5, inner: "" }])
  })
})

describe("openReferenceAt", () => {
  it("is the text between the brace and the cursor", () => {
    // The usual state while typing: there is no closing `}}` yet, so `ranges` sees nothing here.
    expect(openReferenceAt("a {{ llm", 8)).toBe(" llm")
  })

  it("is null outside any reference", () => {
    expect(openReferenceAt("plain text", 5)).toBeNull()
  })

  it("is null once the reference has closed before the cursor", () => {
    expect(openReferenceAt("{{ a }} b", 9)).toBeNull()
  })

  it("is the open one when an earlier reference already closed", () => {
    expect(openReferenceAt("{{ a }} {{ b", 12)).toBe(" b")
  })

  it("is null inside an unclosed comment or block", () => {
    expect(openReferenceAt("{# {{ a", 7)).toBeNull()
    expect(openReferenceAt("{% {{ a", 7)).toBeNull()
  })

  it("still works after a comment that closed", () => {
    expect(openReferenceAt("{# c #} {{ a", 12)).toBe(" a")
  })

  it("reads the cursor position, not the end of the text", () => {
    // The completion source asks about where the cursor is; text after it is irrelevant.
    expect(openReferenceAt("{{ llm_1.text }} tail", 8)).toBe(" llm_1")
  })
})
