import { render, screen, within } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import type { NodeType } from "@/lib/palette"

import { Palette } from "./Palette"

function type(name: string, label: string, category: string): NodeType {
  return { type: name, label, category, isBranch: false, sideEffects: false, configSchema: {}, defaultPolicy: null }
}

const REGISTRY: NodeType[] = [
  type("start", "시작", "IO"),
  type("end", "끝", "IO"),
  type("template", "템플릿", "Logic"),
  type("llm", "LLM", "AI"),
  type("classifier", "분류", "AI"),
  type("condition", "조건", "Logic"),
  type("merge", "합치기", "Logic"),
  type("human_approval", "사람 승인", "Human"),
  type("http_request", "HTTP 요청", "Action"),
]

describe("the palette", () => {
  it("shows every node type the engine registered", () => {
    render(<Palette types={REGISTRY} />)

    for (const item of REGISTRY) {
      expect(screen.getByText(item.label)).toBeInTheDocument()
    }
  })

  it("groups them under Korean headings in the palette's own order", () => {
    render(<Palette types={REGISTRY} />)

    const headings = screen.getAllByRole("heading", { level: 2 }).map((node) => node.textContent)
    expect(headings).toEqual(["입출력", "모델", "흐름", "동작", "사람"])
  })

  it("keeps each type in its own group", () => {
    render(<Palette types={REGISTRY} />)

    const models = screen.getByRole("heading", { name: "모델" }).parentElement
    expect(models).not.toBeNull()
    expect(within(models as HTMLElement).getByText("LLM")).toBeInTheDocument()
    expect(within(models as HTMLElement).queryByText("템플릿")).toBeNull()
  })

  it("makes each entry draggable, since that is the only way to place one", () => {
    render(<Palette types={REGISTRY} />)

    for (const button of screen.getAllByRole("button")) {
      expect(button).toHaveAttribute("draggable", "true")
    }
  })
})
