import { describe, expect, it } from "vitest"

import type { NodeRunState } from "@/lib/run/events"
import type { Issue } from "@/store/validation"

import { movedPositions, toFlowEdges, toFlowNodes } from "./flow"
import type { EditorDsl } from "./document"

const DSL: EditorDsl = {
  version: "1",
  nodes: [
    { id: "start", type: "start", position: { x: 0, y: 0 } },
    { id: "condition_1", type: "condition", label: "점수 확인", position: { x: 100, y: 0 } },
    { id: "end", type: "end", position: { x: 200, y: 0 } },
  ],
  edges: [
    { id: "e1", source: "start", target: "condition_1" },
    { id: "e2", source: "condition_1", sourceHandle: "true", target: "end" },
  ],
}

describe("toFlowNodes", () => {
  it("carries the id, position and label the canvas draws", () => {
    const nodes = toFlowNodes(DSL, {})

    expect(nodes[1]).toMatchObject({
      id: "condition_1",
      position: { x: 100, y: 0 },
      data: { label: "점수 확인", type: "condition" },
    })
  })

  it("falls back to the node type's label when the node has none", () => {
    const nodes = toFlowNodes(DSL, { labels: { start: "시작" } })

    expect(nodes[0]?.data.label).toBe("시작")
  })

  it("falls back to the raw type when even that is unknown", () => {
    // A node type the editor has not heard of still has to draw something readable.
    expect(toFlowNodes(DSL, {})[0]?.data.label).toBe("start")
  })

  it("uses the handles the last analysis reported", () => {
    const nodes = toFlowNodes(DSL, { handles: { condition_1: ["true", "false"] } })

    expect(nodes[1]?.data.handles).toEqual(["true", "false"])
  })

  it("falls back to a single default handle before the first analysis", () => {
    // A node dropped a moment ago has no analysis yet -- and never will while it is unconnected, since
    // HANDLE_NOT_CONNECTED is a phase-2 error that leaves `nodes` empty. It still needs a handle to
    // drag a connection out of, or it can never be connected at all.
    expect(toFlowNodes(DSL, {})[1]?.data.handles).toEqual(["out"])
  })
})

describe("toFlowEdges", () => {
  it("makes the default handle explicit, because React Flow matches on it", () => {
    // The DSL omits `sourceHandle: "out"` so that two documents meaning the same thing hash the same.
    // React Flow has no such rule: an edge whose sourceHandle is undefined will not attach to a Handle
    // rendered with id "out", and the edge silently draws from the node's centre instead.
    const edges = toFlowEdges(DSL)

    expect(edges[0]).toMatchObject({ id: "e1", source: "start", target: "condition_1", sourceHandle: "out" })
    expect(edges[1]?.sourceHandle).toBe("true")
  })
})

describe("movedPositions", () => {
  it("collects position changes by node id", () => {
    const moved = movedPositions([
      { id: "start", type: "position", position: { x: 5, y: 6 }, dragging: true },
      { id: "end", type: "position", position: { x: 7, y: 8 }, dragging: true },
    ])

    expect(moved).toEqual({ start: { x: 5, y: 6 }, end: { x: 7, y: 8 } })
  })

  it("ignores changes that are not moves", () => {
    const moved = movedPositions([
      { id: "start", type: "select", selected: true },
      { id: "end", type: "position", position: { x: 1, y: 2 }, dragging: true },
      { id: "x", type: "remove" },
    ])

    expect(moved).toEqual({ end: { x: 1, y: 2 } })
  })

  it("ignores a position change that carries no position", () => {
    // React Flow emits `{type: "position", dragging: false}` with no position when a drag ends. Reading
    // it as a move would write `undefined` over the node's real coordinates.
    expect(movedPositions([{ id: "start", type: "position", dragging: false }])).toEqual({})
  })
})

describe("validation on the canvas", () => {
  const GRAPH: EditorDsl = {
    version: "1",
    nodes: [
      { id: "cond_1", type: "condition", position: { x: 0, y: 0 } },
      { id: "llm_1", type: "llm", position: { x: 1, y: 1 } },
    ],
    edges: [
      { id: "e1", source: "cond_1", sourceHandle: "yes", target: "llm_1" },
      { id: "e2", source: "llm_1", target: "cond_1" },
    ],
  }

  const ISSUES: Issue[] = [
    { severity: "error", code: "INVALID_CONFIG", message: "설정", nodeId: "llm_1", field: "config.model" },
    { severity: "error", code: "HANDLE_NOT_CONNECTED", message: "연결", nodeId: "cond_1", field: "handles.no" },
    { severity: "warning", code: "TYPE_WARNING", message: "경고", edgeId: "e1" },
  ]

  it("gives each node only its own issues", () => {
    // Every node wearing every badge would make the badges meaningless.
    const nodes = toFlowNodes(GRAPH, { issues: ISSUES })

    expect(nodes.find((node) => node.id === "llm_1")?.data.issues).toHaveLength(1)
    expect(nodes.find((node) => node.id === "cond_1")?.data.issues).toHaveLength(1)
  })

  it("names only the handles a handles.* issue points at", () => {
    const nodes = toFlowNodes(GRAPH, { issues: ISSUES })

    expect(nodes.find((node) => node.id === "cond_1")?.data.unconnectedHandles).toEqual(["no"])
    // `config.model` is a field, not a handle. Reading it as one would mark a handle called
    // "config.model" that does not exist, and leave the real problem unmarked.
    expect(nodes.find((node) => node.id === "llm_1")?.data.unconnectedHandles).toEqual([])
  })

  it("leaves the issue lists empty when there is nothing to say", () => {
    const nodes = toFlowNodes(GRAPH, {})

    expect(nodes.every((node) => node.data.issues.length === 0)).toBe(true)
  })

  it("colours only the edge an issue names", () => {
    const edges = toFlowEdges(GRAPH, { issues: ISSUES })

    expect(edges.find((edge) => edge.id === "e1")?.style?.stroke).toBe("var(--st-waiting)")
    expect(edges.find((edge) => edge.id === "e2")?.style).toBeUndefined()
  })

  it("colours an edge by its worst issue", () => {
    const edges = toFlowEdges(GRAPH, {
      issues: [...ISSUES, { severity: "error", code: "DUPLICATE_EDGE", message: "중복", edgeId: "e1" }],
    })

    expect(edges.find((edge) => edge.id === "e1")?.style?.stroke).toBe("var(--st-failed)")
  })

  it("leaves every edge plain when there are no issues", () => {
    expect(toFlowEdges(GRAPH).every((edge) => edge.style === undefined)).toBe(true)
  })
})

describe("run state on the canvas", () => {
  const GRAPH2: EditorDsl = {
    version: "1",
    nodes: [
      { id: "start", type: "start", position: { x: 0, y: 0 } },
      { id: "llm_1", type: "llm", position: { x: 1, y: 1 } },
    ],
    edges: [],
  }

  const RUNNING: NodeRunState = { status: "running", attempt: 1, tokens: "안녕", error: null, defaulted: false }
  const DONE: NodeRunState = { status: "succeeded", attempt: 1, tokens: "", error: null, defaulted: false }

  it("gives each node its own run state", () => {
    // Every node wearing the first one's status would make the whole canvas light up at once.
    const nodes = toFlowNodes(GRAPH2, { runNodes: { start: DONE, llm_1: RUNNING } })

    expect(nodes.find((node) => node.id === "start")?.data.run).toEqual({ status: "succeeded", tokens: "" })
    expect(nodes.find((node) => node.id === "llm_1")?.data.run).toEqual({ status: "running", tokens: "안녕" })
  })

  it("leaves a node the run has not reached without one", () => {
    const nodes = toFlowNodes(GRAPH2, { runNodes: { start: DONE } })

    expect(nodes.find((node) => node.id === "llm_1")?.data.run).toBeNull()
  })

  it("is null on every node when no run is being watched", () => {
    expect(toFlowNodes(GRAPH2, {}).every((node) => node.data.run === null)).toBe(true)
  })

  it("draws a defaulted finish as the design's sixth state, not as a success", () => {
    // `onError: "default"` means the node produced the fallback. Drawing it green would hide that the
    // workflow ran on a stand-in value.
    const nodes = toFlowNodes(GRAPH2, {
      runNodes: { llm_1: { ...DONE, status: "defaulted", defaulted: true } },
    })

    expect(nodes.find((node) => node.id === "llm_1")?.data.run?.status).toBe("default")
  })
})
