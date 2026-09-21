import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import type { JsonSchema } from "@/lib/template/schema"

import { RunDialog } from "./RunDialog"

const SCHEMA: JsonSchema = {
  type: "object",
  properties: { issue: { type: "string", title: "이슈 번호" }, urgent: { type: "boolean", title: "긴급" } },
  required: ["issue"],
}

function show(over: Partial<Parameters<typeof RunDialog>[0]> = {}) {
  const props = {
    schema: SCHEMA,
    open: true,
    starting: false,
    error: null,
    onSubmit: vi.fn(),
    onCancel: vi.fn(),
    ...over,
  }
  return { ...render(<RunDialog {...props} />), props }
}

describe("RunDialog", () => {
  it("stays closed until it is opened", () => {
    show({ open: false })

    expect(screen.queryByRole("dialog")).toBeNull()
  })

  it("draws a field per property the tenant declared", () => {
    show()

    expect(screen.getByLabelText(/이슈 번호/)).toBeInTheDocument()
    expect(screen.getByLabelText(/긴급/)).toBeInTheDocument()
  })

  it("submits what was filled in", async () => {
    const { props } = show()
    await userEvent.type(screen.getByLabelText(/이슈 번호/), "42")
    await userEvent.click(screen.getByRole("button", { name: "실행" }))

    expect(props.onSubmit).toHaveBeenCalledWith({ issue: "42" })
  })

  it("does not submit while a required value is missing", async () => {
    // Unlike the settings panel, validation here is not display-only: sending a run the engine will
    // refuse wastes a round trip and puts the error somewhere harder to read.
    const { props } = show()
    await userEvent.click(screen.getByRole("button", { name: "실행" }))

    expect(props.onSubmit).not.toHaveBeenCalled()
  })

  it("says it is working and refuses a second press", async () => {
    const { props } = show({ starting: true })
    const submit = screen.getByRole("button", { name: "시작하는 중" })

    expect(submit).toBeDisabled()
    await userEvent.click(submit)
    expect(props.onSubmit).not.toHaveBeenCalled()
  })

  it("shows the engine's refusal inside the dialog, where the inputs still are", async () => {
    show({ error: "실행 입력이 너무 큽니다 (최대 64KB)" })

    expect(screen.getByText("실행 입력이 너무 큽니다 (최대 64KB)")).toBeInTheDocument()
  })

  it("can be cancelled", async () => {
    const { props } = show()
    await userEvent.click(screen.getByRole("button", { name: "취소" }))

    expect(props.onCancel).toHaveBeenCalled()
  })
})
