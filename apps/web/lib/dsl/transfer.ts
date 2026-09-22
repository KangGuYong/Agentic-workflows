import { readDocument } from "./read"
import type { EditorDsl } from "./document"

/** Workflow documents leaving and entering the editor as `.json` files.
 *
 * The file is the DSL and nothing else -- no wrapper, no editor metadata, no id. That is what makes an
 * exported file the same thing as the documents in `examples/`, what lets someone hand-write one, and
 * what keeps a file from carrying a workflow's identity into a place it does not belong.
 *
 * Everything here is pure. Picking a file and saving one are DOM jobs and live in the component; the
 * rules about what may be read and what a file is called are testable and live here.
 */

/** Mirrors the engine's `MAX_DSL_BYTES` (`engine/validator/__init__.py`).
 *
 * A copy, not an import -- the browser cannot read the engine's constants. The engine stays the
 * authority: this only means a file it would certainly refuse is refused here, with a message about
 * the file rather than a 422 about a draft the tenant never managed to create.
 */
export const MAX_IMPORT_BYTES = 512 * 1024

export type ImportResult =
  | { ok: true; dsl: EditorDsl; filledPositions: number }
  | { ok: false; reason: string }

/** Read a file's text as a workflow document.
 *
 * Size is checked **before** `JSON.parse`, because parsing is where a hostile file costs something.
 * Callers should check the `File`'s own size first so a 200MB file is never read into a string at all;
 * this second check covers the text they actually hand over.
 */
export function parseImport(text: string): ImportResult {
  const bytes = new TextEncoder().encode(text).length
  if (bytes > MAX_IMPORT_BYTES) {
    return { ok: false, reason: `파일이 너무 큽니다 (최대 ${Math.floor(MAX_IMPORT_BYTES / 1024)}KB).` }
  }
  if (text.trim() === "") return { ok: false, reason: "파일이 비어 있습니다." }

  let raw: unknown
  try {
    raw = JSON.parse(text)
  } catch {
    // The parser's own message names a byte offset, which tells someone nothing about their file.
    return { ok: false, reason: "JSON 형식이 아닙니다." }
  }

  // The same reader that opens a draft written by the API or an older editor: it fills missing
  // positions or refuses, and it never silently drops a node it could not read.
  const read = readDocument(raw)
  if (!read.ok) return read
  return { ok: true, dsl: read.dsl, filledPositions: read.filledPositions }
}

/** The document as it is written to a file: indented, and with a trailing newline.
 *
 * Indented on purpose. An exported workflow is something people diff, review and hand-edit -- the
 * documents in `examples/` are exactly this shape -- and one long line is none of those things.
 *
 * Takes `unknown` because export writes back **what is stored**, which the editor has not necessarily
 * read: a draft the API wrote is still a document worth exporting, and narrowing the type here would
 * only push a cast to the caller.
 */
export function serialize(dsl: unknown): string {
  return `${JSON.stringify(dsl, null, 2)}\n`
}

/** A workflow name turned into a file name.
 *
 * Whatever the tenant typed as a name reaches a download here, so it is treated as untrusted text: path
 * separators, control characters and the Windows-reserved set are replaced rather than escaped, and the
 * result is length-capped. A name that survives none of that falls back to a fixed one -- an empty or
 * dot-only file name is how a download ends up unnamed or hidden.
 */
export function exportFileName(workflowName: string): string {
  const cleaned = [...workflowName.normalize("NFC")]
    .map((char) => (/[\u0000-\u001f\u007f/\\:*?"<>|]/.test(char) ? " " : char))
    .join("")
    .replace(/\s+/g, " ")
    .trim()
    // Dots *and* spaces, repeatedly: once separators are gone, "../../etc/passwd" is ".. .. etc passwd",
    // and stripping only one run of dots leaves a name starting with "..". A leading dot is what makes a
    // file hidden; a leading space is what makes it look unnamed.
    .replace(/^[.\s]+/, "")
    .slice(0, 80)
    .trim()
  return `${cleaned === "" ? "workflow" : cleaned}.json`
}

/** The workflow name an imported file gets, taken from the file's own name.
 *
 * From the file name and never from the file's contents: the DSL has no name field, and inventing one
 * from something inside an untrusted document would be a way to put arbitrary text in the list.
 */
export function importedName(fileName: string): string {
  const base = fileName.replace(/\.[^.]*$/, "").replace(/\s+/g, " ").trim().slice(0, 80).trim()
  return base === "" ? "가져온 워크플로" : base
}

/** Read a picked file as a workflow document.
 *
 * The `File`'s own size is checked **before** its text is read, so a 200MB file never becomes a 200MB
 * string; `parseImport` then checks the text's byte length, because that is what the engine limits.
 */
export async function readWorkflowFile(file: File): Promise<ImportResult> {
  if (file.size > MAX_IMPORT_BYTES) {
    return { ok: false, reason: `파일이 너무 큽니다 (최대 ${Math.floor(MAX_IMPORT_BYTES / 1024)}KB).` }
  }
  const text = await file.text().catch(() => null)
  if (text === null) return { ok: false, reason: "파일을 읽지 못했습니다." }
  return parseImport(text)
}

/** What opening a file did, in one line a person can read.
 *
 * Import replaces the document on screen, which is the kind of thing that must never be silent: the
 * line says what arrived and that 되돌리기 takes it back, so a mis-picked file is a keystroke to undo
 * rather than a draft to reconstruct.
 */
export function describeLoad(loaded: { nodes: number; edges: number }): string {
  if (loaded.nodes === 0) return "노드가 없는 문서를 불러왔습니다. 되돌리기로 이전 내용을 되살릴 수 있습니다."
  return `노드 ${loaded.nodes}개와 연결 ${loaded.edges}개를 불러왔습니다. 이전 내용은 되돌리기로 되살릴 수 있습니다.`
}
