import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { JsonValue } from "./JsonValue"

describe("JsonValue", () => {
  it("renders the scalars", () => {
    const { container } = render(
      <JsonValue value={{ a: "문자열", b: 42, c: true, d: null }} />,
    )

    expect(container).toHaveTextContent("문자열")
    expect(container).toHaveTextContent("42")
    expect(container).toHaveTextContent("true")
    expect(container).toHaveTextContent("null")
  })

  it("never shows the redaction marker as plain text", () => {
    // Printing it reads as though the secret is *in* the record, and nothing distinguishes a redacted
    // field from one whose value happens to be those ten characters.
    const { container } = render(
      <JsonValue value={{ headers: { Authorization: "[REDACTED]" }, url: "https://example.com" } } />,
    )

    expect(container.textContent).not.toContain("[REDACTED]")
    expect(screen.getByText("가려짐")).toBeInTheDocument()
  })

  it("finds a marker however deeply it is buried", () => {
    const { container } = render(<JsonValue value={{ a: { b: [{ c: "[REDACTED]" }] } }} />)

    expect(container.textContent).not.toContain("[REDACTED]")
    expect(screen.getByText("가려짐")).toBeInTheDocument()
  })

  it("finds it below the depth where the tree stops expanding", () => {
    // Past a few levels the value is printed as JSON instead of as a tree. That printing must not put
    // the marker back on screen.
    const deep = { a: { b: { c: { d: { e: "[REDACTED]" } } } } }
    const { container } = render(<JsonValue value={deep} />)

    expect(container.textContent).not.toContain("[REDACTED]")
  })

  it("labels the badge with a word, not only a lock", () => {
    render(<JsonValue value="[REDACTED]" />)

    expect(screen.getByText("가려짐")).toBeInTheDocument()
  })

  it("shows an empty container as such rather than as nothing", () => {
    const { container } = render(<JsonValue value={{ items: [], meta: {} }} />)

    expect(container).toHaveTextContent("[]")
    expect(container).toHaveTextContent("{}")
  })

  it("keeps the newlines in a multi-line string", () => {
    const { container } = render(<JsonValue value={"첫 줄\n둘째 줄"} />)

    expect(container.querySelector(".whitespace-pre-wrap")?.textContent).toBe("첫 줄\n둘째 줄")
  })
})
