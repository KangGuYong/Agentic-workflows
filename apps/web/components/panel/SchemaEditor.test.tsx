import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { useState } from "react"
import { describe, expect, it, vi } from "vitest"

import type { JsonSchema } from "@/lib/template/schema"

import { SchemaEditor } from "./SchemaEditor"

/** The editor as the panel mounts it: the schema it shows is the schema it last produced. */
function Live({ initial }: { initial?: JsonSchema }) {
  const [value, setValue] = useState<JsonSchema | undefined>(initial)
  return (
    <div>
      <SchemaEditor id="s" value={value} onChange={setValue} />
      <pre data-testid="schema">{JSON.stringify(value)}</pre>
    </div>
  )
}

function schema(): unknown {
  return JSON.parse(screen.getByTestId("schema").textContent || "null")
}

describe("the form", () => {
  it("builds an object schema from two fields, one of them required", async () => {
    render(<Live />)

    await userEvent.click(screen.getByRole("button", { name: "+ 필드 추가" }))
    await userEvent.click(screen.getByRole("button", { name: "+ 필드 추가" }))

    const names = screen.getAllByLabelText("이름")
    await userEvent.clear(names[0] as HTMLElement)
    await userEvent.type(names[0] as HTMLElement, "a")
    await userEvent.clear(names[1] as HTMLElement)
    await userEvent.type(names[1] as HTMLElement, "b")
    await userEvent.click(screen.getAllByLabelText("필수")[0] as HTMLElement)

    expect(schema()).toEqual({
      type: "object",
      properties: { a: { type: "string" }, b: { type: "string" } },
      required: ["a"],
    })
  })

  it("writes an empty object schema, not `{}`, when the last field is removed", async () => {
    render(<Live initial={{ type: "object", properties: { a: { type: "string" } } }} />)

    await userEvent.click(screen.getByRole("button", { name: "삭제" }))

    expect(schema()).toEqual({ type: "object", properties: {} })
  })

  it("offers exactly the types the engine accepts as a field type", async () => {
    render(<Live initial={{ type: "object", properties: { a: { type: "string" } } }} />)

    const options = screen.getAllByRole("option").map((option) => option.textContent)
    expect(options).toEqual(["문자열", "숫자", "정수", "참/거짓", "목록", "객체"])
  })

  it("says when two fields share a name, which the engine cannot see", async () => {
    // A JSON object keeps the last of two identical keys, so the duplicate never reaches `/validate` --
    // the field simply vanishes on save.
    render(<Live initial={{ type: "object", properties: { a: { type: "string" }, b: { type: "string" } } }} />)

    const names = screen.getAllByLabelText("이름")
    await userEvent.clear(names[1] as HTMLElement)
    await userEvent.type(names[1] as HTMLElement, "a")

    // Both rows are marked: either one is the one to rename, and pointing at only the second would
    // suggest the first is the "real" one.
    expect(screen.getAllByText(/이름이 중복됩니다/)).toHaveLength(2)
    expect(screen.getAllByLabelText("이름").every((input) => input.getAttribute("aria-invalid") === "true")).toBe(true)
  })

  it("keeps the shadowed field instead of deleting it while the name is being typed", async () => {
    // The reason the model is state rather than derived: `toSchema` collapses two identical keys, so
    // round-tripping through it on each keystroke would drop the second field the moment its name
    // matched the first -- and typing past the collision could never bring it back.
    render(<Live initial={{ type: "object", properties: { a: { type: "string" }, b: { type: "string" } } }} />)

    const names = screen.getAllByLabelText("이름")
    await userEvent.clear(names[1] as HTMLElement)
    await userEvent.type(names[1] as HTMLElement, "a")
    expect(screen.getAllByLabelText("이름")).toHaveLength(2)

    await userEvent.type(screen.getAllByLabelText("이름")[1] as HTMLElement, "b")

    expect(screen.getAllByLabelText("이름").map((input) => (input as HTMLInputElement).value)).toEqual(["a", "ab"])
    expect(schema()).toEqual({
      type: "object",
      properties: { a: { type: "string" }, ab: { type: "string" } },
    })
  })
})

describe("nesting", () => {
  const NESTED: JsonSchema = {
    type: "object",
    properties: { user: { type: "object", properties: { id: { type: "string" } } } },
  }

  it("opens a nested object and shows a breadcrumb back", async () => {
    render(<Live initial={NESTED} />)

    await userEvent.click(screen.getByRole("button", { name: "하위 항목" }))
    expect(screen.getByLabelText("이름")).toHaveValue("id")

    await userEvent.click(screen.getByRole("button", { name: "전체" }))
    expect(screen.getByLabelText("이름")).toHaveValue("user")
  })

  it("edits inside the nested level, leaving the outer one alone", async () => {
    render(<Live initial={NESTED} />)
    await userEvent.click(screen.getByRole("button", { name: "하위 항목" }))
    await userEvent.click(screen.getByLabelText("필수"))

    expect(schema()).toEqual({
      type: "object",
      properties: {
        user: { type: "object", properties: { id: { type: "string" } }, required: ["id"] },
      },
    })
  })

  it("asks before a type change throws away nested fields", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false)
    render(<Live initial={NESTED} />)

    await userEvent.selectOptions(screen.getByLabelText("형식"), "문자열")

    expect(confirm).toHaveBeenCalled()
    // Declined, so nothing changed.
    expect(schema()).toEqual(NESTED)
    confirm.mockRestore()
  })

  it("does not ask when the change keeps them", async () => {
    // 객체 -> 목록 is a change of arity, not of contents.
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true)
    render(<Live initial={NESTED} />)

    await userEvent.selectOptions(screen.getByLabelText("형식"), "목록")

    expect(confirm).not.toHaveBeenCalled()
    expect(schema()).toEqual({
      type: "object",
      properties: { user: { type: "array", items: { type: "object", properties: { id: { type: "string" } } } } },
    })
    confirm.mockRestore()
  })
})

describe("a schema the form cannot represent", () => {
  const ANY_OF: JsonSchema = {
    type: "object",
    properties: { a: { anyOf: [{ type: "string" }, { type: "number" }] } },
  }

  it("disables the form toggle and says why", () => {
    render(<Live initial={ANY_OF} />)

    expect(screen.getByRole("button", { name: "폼" })).toBeDisabled()
    expect(screen.getByText(/폼으로 다룰 수 없는/)).toBeInTheDocument()
  })

  it("keeps the JSON intact through a round trip", async () => {
    // The whole point: an editor that quietly dropped `anyOf` would change what the workflow accepts,
    // and nobody would find out until a run produced the wrong shape.
    render(<Live initial={ANY_OF} />)

    const textarea = screen.getByRole("textbox")
    expect(JSON.parse((textarea as HTMLTextAreaElement).value)).toEqual(ANY_OF)

    await userEvent.click(textarea)
    await userEvent.tab()

    expect(schema()).toEqual(ANY_OF)
  })
})

describe("the JSON mode", () => {
  it("is reachable from the form and writes what was typed", async () => {
    render(<Live />)

    await userEvent.click(screen.getByRole("button", { name: "JSON" }))
    const textarea = screen.getByRole("textbox")
    await userEvent.type(textarea, '{{"type": "object", "properties": {{}}')
    await userEvent.tab()

    expect(schema()).toEqual({ type: "object", properties: {} })
  })

  it("leaves unparseable text exactly as typed and says so", async () => {
    render(<Live initial={{ type: "object", properties: {} }} />)
    await userEvent.click(screen.getByRole("button", { name: "JSON" }))

    const textarea = screen.getByRole("textbox")
    await userEvent.clear(textarea)
    await userEvent.type(textarea, "{{not json")
    await userEvent.tab()

    expect(screen.getByText(/JSON 형식이 아닙니다/)).toBeInTheDocument()
    expect((textarea as HTMLTextAreaElement).value).toBe("{not json")
    // And the schema is untouched -- text that is not JSON cannot replace one that is.
    expect(schema()).toEqual({ type: "object", properties: {} })
  })

  it("clears the schema when the box is emptied", async () => {
    render(<Live initial={{ type: "object", properties: {} }} />)
    await userEvent.click(screen.getByRole("button", { name: "JSON" }))

    await userEvent.clear(screen.getByRole("textbox"))
    await userEvent.tab()

    expect(schema()).toBeNull()
  })
})
