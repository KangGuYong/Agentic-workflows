import { StrictMode, useState } from "react"
import { createRoot } from "react-dom/client"

import { TemplateEditor } from "@/components/panel/TemplateEditor"
import type { TemplateContext } from "@/lib/template/context"

/** A page holding nothing but the template editor.
 *
 * The IME test needs a real browser and a real CodeMirror, and it needs neither Next, the engine, nor a
 * database to say anything about composition. Mounting the component alone keeps the test about the
 * editor: a failure here is the editor's, not a stack that did not come up.
 */

const CONTEXT: TemplateContext = {
  nodes: [
    { id: "start", label: "시작", outputSchema: { type: "object", properties: { name: { type: "string" } } } },
    {
      id: "llm_1",
      label: "요약",
      outputSchema: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
    },
  ],
  guaranteed: ["start"],
}

function Fixture() {
  const [value, setValue] = useState("")
  // `?lag=1` commits a frame late, which is what any parent that batches, debounces or round-trips
  // through a store does. It is the only condition under which the editor's `value` effect sees a
  // document that differs from its prop -- and so the only condition that can clobber an open IME
  // composition. Without it the guard against that is untestable, and untestable defensive code is
  // indistinguishable from dead code.
  const lag = new URLSearchParams(window.location.search).get("lag") === "1"
  const commit = (next: string) => {
    if (lag) requestAnimationFrame(() => setValue(next))
    else setValue(next)
  }

  return (
    <div style={{ width: 420 }}>
      <TemplateEditor id="field" value={value} context={CONTEXT} onChange={commit} onBlur={() => {}} />
      {/* The test reads the document from here rather than from the DOM CodeMirror renders: chips
          replace the text they cover, so the rendered text is deliberately not the document. */}
      <pre data-testid="value">{value}</pre>
    </div>
  )
}

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <Fixture />
  </StrictMode>,
)
