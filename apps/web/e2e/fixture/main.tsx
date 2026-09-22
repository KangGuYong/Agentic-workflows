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

/** Longer than the four CDP round-trips that compose and commit one syllable, which take ~15ms. */
const LAG_MS = 50

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
  // Every document the parent committed, in order. The IME test asserts on this rather than on the
  // final value alone: a stale write-back cycles the value through the same documents again, and a
  // polling assertion on the value can catch the expected one in passing. The log cannot match twice.
  const [commits, setCommits] = useState<string[]>([])
  // `?lag=1` commits late, which is what any parent that batches, debounces or round-trips through a
  // store does. It is the only condition under which the editor's `value` effect sees a document that
  // differs from its prop -- and so the only condition under which the editor can write a stale
  // document back over a syllable the IME just composed. Without it the editor's defence against that
  // is untestable, and untestable defensive code is indistinguishable from dead code.
  //
  // Late by longer than a whole syllable takes to compose, so that every report of that syllable is
  // still in flight when the first commit lands. A one-frame lag reproduced the race only under load,
  // roughly once in twenty runs; this reproduces it every run.
  const lag = new URLSearchParams(window.location.search).get("lag") === "1"
  const apply = (next: string) => {
    setValue(next)
    setCommits((seen) => [...seen, next])
  }
  const commit = (next: string) => {
    if (lag) setTimeout(() => apply(next), LAG_MS)
    else apply(next)
  }

  return (
    <div style={{ width: 420 }}>
      <TemplateEditor id="field" value={value} context={CONTEXT} onChange={commit} onBlur={() => {}} />
      {/* The test reads the document from here rather than from the DOM CodeMirror renders: chips
          replace the text they cover, so the rendered text is deliberately not the document. */}
      <pre data-testid="value">{value}</pre>
      <pre data-testid="commits">{commits.join(",")}</pre>
    </div>
  )
}

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <Fixture />
  </StrictMode>,
)
