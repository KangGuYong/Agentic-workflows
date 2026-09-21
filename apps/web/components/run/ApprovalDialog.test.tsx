import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import type { WaitingFor } from "@/lib/run/events"

import { ApprovalDialog } from "./ApprovalDialog"

const WAITING: WaitingFor = {
  nodeId: "approval_1",
  execIndex: 1,
  message: "이 요약을 승인해 주세요",
  review: { text: "요약본", tokens: 120 },
  allowEdit: false,
}

function show(over: Partial<Parameters<typeof ApprovalDialog>[0]> = {}) {
  const props = {
    waiting: WAITING,
    open: true,
    submitting: false,
    error: null,
    onSubmit: vi.fn(),
    onDismiss: vi.fn(),
    ...over,
  }
  return { ...render(<ApprovalDialog {...props} />), props }
}

describe("what it shows", () => {
  it("stays closed until opened", () => {
    show({ open: false })

    expect(screen.queryByRole("dialog")).toBeNull()
  })

  it("shows the message the node rendered", () => {
    show()

    expect(screen.getByText("이 요약을 승인해 주세요")).toBeInTheDocument()
  })

  it("shows the value to review", () => {
    show()

    expect(screen.getByText("요약본")).toBeInTheDocument()
  })

  it("shows no review section when the node sends none", () => {
    show({ waiting: { ...WAITING, review: null } })

    expect(screen.queryByText("검토할 값")).toBeNull()
  })

  it("never shows a redaction marker as plain text in the review", () => {
    const { container } = show({ waiting: { ...WAITING, review: { token: "[REDACTED]" } } })

    expect(container.textContent).not.toContain("[REDACTED]")
  })
})

describe("editing the value", () => {
  it("is not offered unless the node allowed it", () => {
    // The engine validates the edited value against the same rule and rejects one it did not invite,
    // so offering the box anyway would only produce a 422.
    show()

    expect(screen.queryByLabelText("검토할 값")).toBeNull()
  })

  it("is offered when it was", () => {
    show({ waiting: { ...WAITING, allowEdit: true } })

    expect(screen.getByLabelText("검토할 값")).toBeInTheDocument()
  })

  it("sends the edited value when it was changed", async () => {
    const { props } = show({ waiting: { ...WAITING, allowEdit: true } })
    const box = screen.getByLabelText("검토할 값")
    await userEvent.clear(box)
    await userEvent.type(box, '{{"text": "고친 요약"}')
    await userEvent.click(screen.getByRole("button", { name: "승인" }))

    expect(props.onSubmit).toHaveBeenCalledWith("approve", "", { text: "고친 요약" })
  })

  it("sends no edited value when it was left alone", async () => {
    // Sending the original back would be indistinguishable from an edit that happened to match.
    const { props } = show({ waiting: { ...WAITING, allowEdit: true } })
    await userEvent.click(screen.getByRole("button", { name: "승인" }))

    expect(props.onSubmit).toHaveBeenCalledWith("approve", "")
  })

  it("refuses to send text that is not JSON, and keeps it on screen", async () => {
    const { props } = show({ waiting: { ...WAITING, allowEdit: true } })
    const box = screen.getByLabelText("검토할 값")
    await userEvent.clear(box)
    await userEvent.type(box, "{{고장난")
    await userEvent.click(screen.getByRole("button", { name: "승인" }))

    expect(props.onSubmit).not.toHaveBeenCalled()
    expect(screen.getByText(/올바른 JSON이 아닙니다/)).toBeInTheDocument()
    expect((box as HTMLTextAreaElement).value).toBe("{고장난")
  })
})

describe("deciding", () => {
  it("sends an approval with the comment", async () => {
    const { props } = show()
    await userEvent.type(screen.getByLabelText(/의견/), "좋습니다")
    await userEvent.click(screen.getByRole("button", { name: "승인" }))

    expect(props.onSubmit).toHaveBeenCalledWith("approve", "좋습니다")
  })

  it("sends a rejection the same way", async () => {
    const { props } = show()
    await userEvent.type(screen.getByLabelText(/의견/), "내용이 부족합니다")
    await userEvent.click(screen.getByRole("button", { name: "반려" }))

    expect(props.onSubmit).toHaveBeenCalledWith("reject", "내용이 부족합니다")
  })

  it("refuses a second press while one is in the air", async () => {
    const { props } = show({ submitting: true })
    await userEvent.click(screen.getByRole("button", { name: "보내는 중" }))

    expect(props.onSubmit).not.toHaveBeenCalled()
  })

  it("shows the engine's refusal, which is already Korean", () => {
    show({ error: "잘못된 승인 응답입니다: 객체가 아닙니다" })

    expect(screen.getByText("잘못된 승인 응답입니다: 객체가 아닙니다")).toBeInTheDocument()
  })

  it("can be put off without answering", async () => {
    // The run stays parked either way; the dialog reopens from the node.
    const { props } = show()
    await userEvent.click(screen.getByRole("button", { name: "나중에" }))

    expect(props.onDismiss).toHaveBeenCalled()
    expect(props.onSubmit).not.toHaveBeenCalled()
  })
})
