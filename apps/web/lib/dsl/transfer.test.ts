import { describe, expect, it, vi } from "vitest"

import type { EditorDsl } from "./document"
import {
  MAX_IMPORT_BYTES,
  describeLoad,
  exportFileName,
  importedName,
  parseImport,
  readWorkflowFile,
  serialize,
} from "./transfer"

const DSL: EditorDsl = {
  version: "1",
  nodes: [
    { id: "start", type: "start", position: { x: 0, y: 0 } },
    { id: "end", type: "end", position: { x: 200, y: 0 } },
  ],
  edges: [{ id: "e1", source: "start", target: "end" }],
}

describe("parseImport", () => {
  it("reads a document the editor wrote", () => {
    const result = parseImport(serialize(DSL))

    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.dsl).toEqual(DSL)
      expect(result.filledPositions).toBe(0)
    }
  })

  it("places nodes that have no position", () => {
    // A hand-written file, or one produced by the API. Positions are editor-only and the engine does
    // not store them, so a file without them is normal -- not an error.
    const result = parseImport(JSON.stringify({ version: "1", nodes: [{ id: "a", type: "start" }], edges: [] }))

    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.filledPositions).toBe(1)
      expect(result.dsl.nodes[0]?.position).toEqual({ x: 80, y: 80 })
    }
  })

  it("refuses a file that is not JSON, without quoting the parser", () => {
    const result = parseImport("{ nodes: [] }")

    expect(result.ok).toBe(false)
    // "Unexpected token n in JSON at position 2" names a byte offset, which tells nobody anything.
    if (!result.ok) expect(result.reason).toBe("JSON 형식이 아닙니다.")
  })

  it("refuses an empty file", () => {
    expect(parseImport("   \n ")).toEqual({ ok: false, reason: "파일이 비어 있습니다." })
  })

  it("refuses a document whose shape is wrong, and says which node", () => {
    const result = parseImport(JSON.stringify({ nodes: [{ id: "a", type: "start" }, { oops: true }], edges: [] }))

    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.reason).toContain("2번째 노드")
  })

  it("refuses a file over the engine's own limit before parsing it", () => {
    // One byte over. The engine would answer 422 for this draft; saying so about the *file* is the
    // difference between a message someone can act on and one about a draft that never existed.
    const padding = "x".repeat(MAX_IMPORT_BYTES)
    const result = parseImport(JSON.stringify({ version: "1", nodes: [], edges: [], note: padding }))

    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.reason).toContain("너무 큽니다")
  })

  it("measures the limit in bytes, not characters", () => {
    // Korean is three bytes per character in UTF-8. A character count would let a file three times the
    // engine's limit through, and the 422 would land on the save instead.
    const wide = "가".repeat(MAX_IMPORT_BYTES / 3)
    const result = parseImport(JSON.stringify({ version: "1", nodes: [], edges: [], note: wide }))

    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.reason).toContain("너무 큽니다")
  })

  it("keeps keys it does not know about", () => {
    // `settings`, and whatever a newer engine adds. Dropping them would quietly rewrite the document.
    const result = parseImport(JSON.stringify({ ...DSL, settings: { storeRunData: false } }))

    expect(result.ok).toBe(true)
    if (result.ok) expect((result.dsl as unknown as Record<string, unknown>)["settings"]).toEqual({ storeRunData: false })
  })
})

describe("serialize", () => {
  it("writes an indented document ending in a newline", () => {
    const text = serialize(DSL)

    expect(text.startsWith("{\n  ")).toBe(true)
    expect(text.endsWith("\n")).toBe(true)
  })

  it("round-trips", () => {
    const back = parseImport(serialize(DSL))

    expect(back.ok && back.dsl).toEqual(DSL)
  })
})

describe("exportFileName", () => {
  it("keeps a normal name, Korean included", () => {
    expect(exportFileName("주문 처리")).toBe("주문 처리.json")
  })

  it("strips path separators so a name cannot steer where the file lands", () => {
    expect(exportFileName("../../etc/passwd")).toBe("etc passwd.json")
  })

  it("strips the characters Windows refuses in a file name", () => {
    expect(exportFileName('a:b*c?d"e<f>g|h')).toBe("a b c d e f g h.json")
  })

  it("removes control characters", () => {
    expect(exportFileName("a\u0000b\nc")).toBe("a b c.json")
  })

  it("does not produce a hidden file from a leading dot", () => {
    expect(exportFileName(".bashrc")).toBe("bashrc.json")
  })

  it("falls back when nothing survives", () => {
    expect(exportFileName("///")).toBe("workflow.json")
    expect(exportFileName("   ")).toBe("workflow.json")
    expect(exportFileName("...")).toBe("workflow.json")
  })

  it("caps the length without leaving a trailing space", () => {
    const name = exportFileName(`${"가".repeat(79)} ${"나".repeat(20)}`)

    expect(name.endsWith(" .json")).toBe(false)
    expect(name.length).toBeLessThanOrEqual(85)
  })
})

describe("importedName", () => {
  it("uses the file's base name", () => {
    expect(importedName("01-hello.json")).toBe("01-hello")
  })

  it("keeps a name that has dots inside it", () => {
    expect(importedName("v1.2 주문.json")).toBe("v1.2 주문")
  })

  it("falls back for a nameless file", () => {
    expect(importedName(".json")).toBe("가져온 워크플로")
    expect(importedName("")).toBe("가져온 워크플로")
  })
})

describe("readWorkflowFile", () => {
  function file(name: string, body: string, size?: number) {
    const made = new File([body], name, { type: "application/json" })
    if (size !== undefined) Object.defineProperty(made, "size", { value: size })
    return made
  }

  it("reads a workflow file", async () => {
    const result = await readWorkflowFile(file("a.json", serialize(DSL)))

    expect(result.ok && result.dsl).toEqual(DSL)
  })

  it("refuses an oversized file without reading it", async () => {
    // The whole reason `File.size` is checked separately: a huge file must never become a huge string.
    const read = vi.spyOn(File.prototype, "text")

    const result = await readWorkflowFile(file("huge.json", "{}", MAX_IMPORT_BYTES + 1))

    expect(result.ok).toBe(false)
    expect(read).not.toHaveBeenCalled()
    read.mockRestore()
  })

  it("passes a bad document's reason through", async () => {
    expect(await readWorkflowFile(file("a.json", "{ nope }"))).toEqual({
      ok: false,
      reason: "JSON 형식이 아닙니다.",
    })
  })
})

describe("describeLoad", () => {
  it("counts what arrived, nodes and connections both", () => {
    // Connections are the half that was missing when import used to add rather than replace, so they
    // are worth naming.
    expect(describeLoad({ nodes: 5, edges: 4 })).toContain("노드 5개와 연결 4개")
  })

  it("always says undo brings the old document back", () => {
    // Import replaces what was on screen. Saying so is what turns a mis-picked file into a keystroke.
    expect(describeLoad({ nodes: 5, edges: 4 })).toContain("되돌리기")
    expect(describeLoad({ nodes: 0, edges: 0 })).toContain("되돌리기")
  })

  it("says plainly when the file had no nodes", () => {
    expect(describeLoad({ nodes: 0, edges: 0 })).toContain("노드가 없는 문서")
  })
})
