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
