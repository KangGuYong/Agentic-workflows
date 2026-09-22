import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import type { WidgetProps } from "@rjsf/utils"

import { KnowledgeBaseWidget } from "./KnowledgeBaseWidget"

const BASES = [
  { id: "kb_1", name: "제품 문서", embedModel: "bge-m3", fileCount: 1, createdAt: "" },
  { id: "kb_2", name: "사내 규정", embedModel: "bge-m3", fileCount: 3, createdAt: "" },
]

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify({ knowledgeBases: BASES }), { status: 200 }))))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function props(overrides: Partial<WidgetProps> = {}): WidgetProps {
  return { id: "root_knowledgeBase", value: undefined, onChange: vi.fn(), onBlur: vi.fn(), onFocus: vi.fn(), ...overrides } as unknown as WidgetProps
}

it("lists the knowledge bases by name and writes the chosen id", async () => {
  const onChange = vi.fn()
  render(<KnowledgeBaseWidget {...props({ onChange })} />)

  const select = await screen.findByRole("combobox")
  await userEvent.selectOptions(select, "kb_2")

  expect(screen.getByRole("option", { name: "사내 규정" })).toBeInTheDocument()
  expect(onChange).toHaveBeenCalledWith("kb_2")
})

it("keeps a saved id that is no longer listed, and says so", async () => {
  render(<KnowledgeBaseWidget {...props({ value: "kb_gone" })} />)
  const select = await screen.findByRole("combobox")
  expect((select as HTMLSelectElement).value).toBe("kb_gone")
  expect(screen.getByRole("option", { name: /삭제된 지식베이스/ })).toBeInTheDocument()
})
