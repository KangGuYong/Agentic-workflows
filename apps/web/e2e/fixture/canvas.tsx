import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import { Canvas } from "@/components/canvas/Canvas"
import type { EditorDsl } from "@/lib/dsl/document"
import type { NodeType } from "@/lib/palette"

// The app's own stylesheet, because this page is about layout: the palette, the toolbar and the panel
// all sit over or beside the canvas, and a click that lands on one of them instead of a node is the
// difference between the test and the bug.
import "@/app/globals.css"

/** The editor canvas in a real browser, with no engine behind it.
 *
 * Selection is React Flow's, and React Flow is where jsdom stops: its nodes are measured by a
 * ResizeObserver and its clicks are pointer events over measured nodes. What this page answers is
 * whether the node panel follows a click -- every time, not only until the graph has been re-derived.
 * Without a workflow id nothing saves, validates or runs, which is fine: none of that is selection.
 */

const TYPES: NodeType[] = [
  { type: "start", label: "시작", category: "IO", isBranch: false, sideEffects: false, configSchema: {}, defaultPolicy: null },
  { type: "end", label: "끝", category: "IO", isBranch: false, sideEffects: false, configSchema: {}, defaultPolicy: null },
  {
    type: "llm",
    label: "LLM",
    category: "AI",
    isBranch: false,
    sideEffects: false,
    configSchema: { type: "object", properties: { prompt: { type: "string", title: "프롬프트" } } },
    defaultPolicy: null,
  },
]

const DSL: EditorDsl = {
  version: "1",
  nodes: [
    { id: "start", type: "start", position: { x: 40, y: 120 } },
    { id: "llm_1", type: "llm", position: { x: 320, y: 120 } },
    { id: "end", type: "end", position: { x: 600, y: 120 } },
  ],
  edges: [
    { id: "e1", source: "start", target: "llm_1" },
    { id: "e2", source: "llm_1", target: "end" },
  ],
}

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <div className="flex h-screen">
      <Canvas types={TYPES} initialDsl={DSL} />
    </div>
  </StrictMode>,
)
