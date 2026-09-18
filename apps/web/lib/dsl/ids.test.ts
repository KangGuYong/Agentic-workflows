import { describe, expect, it } from "vitest"

import type { EditorDsl } from "./document"
import { nextEdgeId, nextNodeId } from "./ids"

function dsl(nodeIds: string[], edgeIds: string[] = []): EditorDsl {
  return {
    version: "1",
    nodes: nodeIds.map((id) => ({ id, type: id.replace(/_\d+$/, ""), position: { x: 0, y: 0 } })),
    edges: edgeIds.map((id) => ({ id, source: "start", target: "end" })),
  }
}

describe("nextNodeId", () => {
  it("numbers from one on an empty document", () => {
    expect(nextNodeId("llm", dsl([]))).toBe("llm_1")
  })

  it("continues from the highest number in use", () => {
    expect(nextNodeId("llm", dsl(["llm_1", "llm_2"]))).toBe("llm_3")
  })

  it("never reuses a number freed by a deletion", () => {
    // MVP 설계 4.2: an id is permanent, and `node_runs.node_id` keeps pointing at it after the node is
    // gone. Reusing `llm_2` would make a new node inherit a deleted one's run history in every trace
    // panel and every stored event.
    expect(nextNodeId("llm", dsl(["llm_2"]))).toBe("llm_3")
    expect(nextNodeId("llm", dsl(["llm_1", "llm_5"]))).toBe("llm_6")
  })

  it("counts only its own type", () => {
    expect(nextNodeId("llm", dsl(["template_7", "http_request_3"]))).toBe("llm_1")
  })

  it("is not confused by a type whose name is a prefix of another", () => {
    // `http_request_1` must not be read as an `http` node numbered `request_1`.
    expect(nextNodeId("http_request", dsl(["http_request_1"]))).toBe("http_request_2")
    expect(nextNodeId("http", dsl(["http_request_9"]))).toBe("http_1")
  })

  it("ignores an id that does not follow the convention", () => {
    // A DSL can arrive from an import or an older editor. `llm_01` is not `llm_1` -- they are different
    // strings and can coexist -- so it contributes no number, and numbering starts at 1 as if it were
    // any other stray id.
    expect(nextNodeId("llm", dsl(["llm_x", "llm_", "llm_01"]))).toBe("llm_1")
  })

  it("never returns an id that is already in use", () => {
    // The real requirement. Parsing `<type>_<n>` is a heuristic for picking a *nice* number; not
    // colliding is what correctness depends on, so it is guaranteed directly rather than inferred from
    // the format rules. A hand-edited or imported document can hold anything.
    const awkward = dsl(["llm_1", "llm_2", "llm_x"])
    awkward.nodes.push({ id: "llm_4", type: "llm", position: { x: 0, y: 0 } })
    expect(nextNodeId("llm", awkward)).toBe("llm_5")

    // A document that already holds the id our numbering would pick.
    const shadowed = dsl(["llm_01"])
    shadowed.nodes.push({ id: "llm_1", type: "template", position: { x: 0, y: 0 } })
    const chosen = nextNodeId("llm", shadowed)
    expect(shadowed.nodes.map((node) => node.id)).not.toContain(chosen)
  })

  it("gives start and end no number, since there is exactly one of each", () => {
    expect(nextNodeId("start", dsl([]))).toBe("start")
    expect(nextNodeId("end", dsl([]))).toBe("end")
  })
})

describe("nextEdgeId", () => {
  it("does not collide with an existing edge", () => {
    expect(nextEdgeId(dsl([], ["e1", "e2"]))).toBe("e3")
    expect(nextEdgeId(dsl([], []))).toBe("e1")
  })

  it("survives edge ids that are not e-numbers", () => {
    expect(nextEdgeId(dsl([], ["edge-from-import", "e4"]))).toBe("e5")
  })
})
