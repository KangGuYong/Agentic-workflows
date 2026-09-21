import { ReactFlowProvider, type NodeProps } from "@xyflow/react"
import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import type { FlowNodeData } from "@/lib/dsl/flow"
import type { Issue } from "@/store/validation"

import { WorkflowNode } from "./WorkflowNode"

function show(data: Partial<FlowNodeData>, selected = false) {
  const full: FlowNodeData = {
    type: "condition",
    label: "조건",
    handles: ["yes", "no"],
    issues: [],
    unconnectedHandles: [],
    run: null,
    ...data,
  }
  // `NodeProps` carries a dozen fields of React Flow plumbing this component never reads. Only the
  // three it does are real here; the rest would be noise that has to be kept in step for nothing.
  const props = { id: "cond_1", data: full, selected } as unknown as NodeProps

  return render(
    <ReactFlowProvider>
      <WorkflowNode {...props} />
    </ReactFlowProvider>,
  )
}

function issue(over: Partial<Issue> = {}): Issue {
  return { severity: "error", code: "INVALID_CONFIG", message: "설정 오류", ...over }
}

describe("the node's validation state", () => {
  it("wears no badge when the engine has nothing to say", () => {
    show({})

    expect(screen.queryByRole("img")).toBeNull()
  })

  it("wears an error badge, and the border says so too", () => {
    // Colour is the glance-level signal; the badge is the readable one. Both, because a border alone
    // cannot carry a message and a badge alone is easy to miss on a busy canvas.
    const { container } = show({ issues: [issue()] })

    expect(screen.getByRole("img")).toHaveAccessibleName(/오류/)
    expect((container.firstChild as HTMLElement).style.borderColor).toBe("var(--st-failed)")
  })

  it("lets an error outrank the selection outline", () => {
    // A node can be both selected and broken. Which one is more urgent to see is not a close call.
    const { container } = show({ issues: [issue()] }, true)

    expect((container.firstChild as HTMLElement).style.borderColor).toBe("var(--st-failed)")
  })

  it("uses the warning colour when that is the worst of it", () => {
    const { container } = show({ issues: [issue({ severity: "warning" })] })

    expect((container.firstChild as HTMLElement).style.borderColor).toBe("var(--st-waiting)")
  })

  it("marks the one handle a HANDLE_NOT_CONNECTED names", () => {
    // `handles.no` addresses one output. Marking the node alone says "something is wrong here";
    // marking the handle says "connect this one" -- which is the whole content of that error.
    const { container } = show({
      issues: [issue({ code: "HANDLE_NOT_CONNECTED", field: "handles.no" })],
      unconnectedHandles: ["no"],
    })

    const marked = container.querySelector<HTMLElement>('[data-handleid="no"]')
    const other = container.querySelector<HTMLElement>('[data-handleid="yes"]')
    expect(marked?.style.background).toBe("var(--st-failed)")
    expect(other?.style.background).not.toBe("var(--st-failed)")
  })

  it("leaves every handle alone when none is named", () => {
    const { container } = show({ issues: [issue()] })

    for (const handle of container.querySelectorAll<HTMLElement>("[data-handleid]")) {
      expect(handle.style.background).not.toBe("var(--st-failed)")
    }
  })
})

describe("the node's run state", () => {
  it("draws nothing extra when no run is being watched", () => {
    const { container } = show({ issues: [] })

    expect(screen.queryByRole("img")).toBeNull()
    expect((container.firstChild as HTMLElement).style.borderColor).toBe("var(--ink-600)")
  })

  it("shows the run status with a glyph, not only a colour", () => {
    show({ run: { status: "running", tokens: "" } })

    const chip = screen.getByRole("img")
    expect(chip).toHaveAccessibleName("실행 상태: 실행 중")
    expect(chip).toHaveTextContent("◍")
  })

  it("names each status it can draw", () => {
    for (const [status, label] of [
      ["running", "실행 중"],
      ["succeeded", "성공"],
      ["failed", "실패"],
      ["waiting", "승인 대기"],
      ["default", "기본값"],
    ] as const) {
      const { unmount } = show({ run: { status, tokens: "" } })
      expect(screen.getByRole("img"), status).toHaveAccessibleName(`실행 상태: ${label}`)
      unmount()
    }
  })

  it("lets the run state outrank a validation badge", () => {
    // During a run, the run is what someone is watching; the validation colour is about a document
    // they are not editing at that moment.
    show({ run: { status: "running", tokens: "" }, issues: [issue()] })

    expect(screen.getByRole("img")).toHaveAccessibleName(/실행 상태/)
    expect(screen.queryByRole("img", { name: /오류/ })).toBeNull()
  })

  it("shows the streamed text", () => {
    show({ run: { status: "running", tokens: "안녕하세요" } })

    expect(screen.getByText("안녕하세요")).toBeInTheDocument()
  })

  it("shows only the tail of a long answer", () => {
    // A node streaming a long answer would otherwise grow until it covered the canvas. What someone
    // watches for is that text is still arriving, which the tail shows just as well.
    const long = "가".repeat(500)
    const { container } = show({ run: { status: "running", tokens: long } })

    expect(container.textContent?.length).toBeLessThan(300)
  })

  it("shows no readout before any token has arrived", () => {
    const { container } = show({ run: { status: "running", tokens: "" } })

    expect(container.querySelector(".readout")).toBeNull()
  })
})
