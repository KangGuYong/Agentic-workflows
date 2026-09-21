import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import type { Issue } from "@/store/validation"

import { Badge, IssueList } from "./Badge"
import { Banner } from "./Banner"

function issue(over: Partial<Issue> = {}): Issue {
  return { severity: "error", code: "INVALID_CONFIG", message: "설정 오류: Field required", ...over }
}

describe("Badge", () => {
  it("translates the pydantic message it shows", () => {
    render(<Badge issues={[issue()]} severity="error" />)

    expect(screen.getByRole("img")).toHaveAccessibleName("오류: 설정 오류: 반드시 입력해야 합니다")
  })

  it("counts the rest instead of concatenating every sentence", () => {
    // Five sentences in a `title` is something nobody reads.
    render(<Badge issues={[issue(), issue(), issue()]} severity="error" />)

    expect(screen.getByRole("img")).toHaveAccessibleName(/외 2건/)
  })

  it("carries the severity in a glyph, not only in colour", () => {
    // A red dot and a yellow dot are the same dot to a colour-blind reader.
    const { unmount } = render(<Badge issues={[issue()]} severity="error" />)
    expect(screen.getByRole("img")).toHaveTextContent("!")
    unmount()

    render(<Badge issues={[issue({ severity: "warning" })]} severity="warning" />)
    expect(screen.getByRole("img")).toHaveTextContent("?")
  })

  it("shows the count once there is more than one", () => {
    render(<Badge issues={[issue(), issue()]} severity="error" />)

    expect(screen.getByRole("img")).toHaveTextContent("2")
  })

  it("names the severity in Korean for a screen reader", () => {
    render(<Badge issues={[issue({ severity: "warning", message: "경고" })]} severity="warning" />)

    expect(screen.getByRole("img")).toHaveAccessibleName("경고: 경고")
  })
})

describe("IssueList", () => {
  it("renders nothing when there is nothing to say", () => {
    const { container } = render(<IssueList issues={[]} />)

    expect(container).toBeEmptyDOMElement()
  })

  it("translates every message it lists", () => {
    render(<IssueList issues={[issue(), issue({ message: "설정 오류: Extra inputs are not permitted" })]} />)

    expect(screen.getByText("설정 오류: 반드시 입력해야 합니다")).toBeInTheDocument()
    expect(screen.getByText("설정 오류: 이 항목은 사용할 수 없습니다")).toBeInTheDocument()
  })
})

describe("Banner", () => {
  it("renders nothing when no issue belongs to the workflow as a whole", () => {
    const { container } = render(<Banner issues={[]} />)

    expect(container).toBeEmptyDOMElement()
  })

  it("announces a workflow-wide problem", () => {
    render(<Banner issues={[issue({ code: "LIMIT_EXCEEDED", message: "워크플로가 너무 큽니다 (최대 512KB)" })]} />)

    expect(screen.getByRole("alert")).toHaveTextContent("워크플로가 너무 큽니다")
  })
})
