import { MAX_IMPORT_BYTES, importedName, parseImport } from "@/lib/dsl/transfer"

import { createWorkflow, putWorkflow } from "./workflows"

/** Turning a picked file into a workflow (Task 23).
 *
 * Shared by the list and the editor so there is one answer to "what does importing do", not two that
 * drift. Both screens offer it and both mean the same thing: **a new workflow**, never a replacement
 * of whatever is open. A file picker is one mis-click from the wrong file, and overwriting a draft
 * with it would be destructive with no undo; creating means the worst case is one workflow to delete.
 */

export type ImportOutcome =
  | { outcome: "ok"; id: string; name: string }
  /** The file was refused before anything was created. Nothing exists to clean up. */
  | { outcome: "rejected"; message: string }
  /** The workflow exists but is empty: the create succeeded and the document did not save. */
  | { outcome: "partial"; id: string; name: string; message: string }

export async function importWorkflowFile(file: File): Promise<ImportOutcome> {
  // The `File`'s own size first, so a 200MB file never becomes a 200MB string. `parseImport` checks
  // the text's byte length too; this one is about never reading it.
  if (file.size > MAX_IMPORT_BYTES) {
    return { outcome: "rejected", message: `파일이 너무 큽니다 (최대 ${Math.floor(MAX_IMPORT_BYTES / 1024)}KB).` }
  }

  const text = await file.text().catch(() => null)
  if (text === null) return { outcome: "rejected", message: "파일을 읽지 못했습니다." }
  const read = parseImport(text)
  if (!read.ok) return { outcome: "rejected", message: read.reason }

  const name = importedName(file.name)
  const created = await createWorkflow(name)
  if (created.outcome !== "ok") return { outcome: "rejected", message: created.message }

  // Two calls because `POST` takes only a name; the document goes in the `PUT` that follows. A failed
  // `PUT` leaves the empty workflow behind rather than deleting it -- deleting here is how an import
  // that half succeeded disappears without a trace, and the name is what lets someone find it.
  const saved = await putWorkflow(created.value.id, name, read.dsl, created.value.revision)
  if (saved.outcome !== "ok") {
    return {
      outcome: "partial",
      id: created.value.id,
      name,
      message: `${saved.message} '${name}' 워크플로는 비어 있는 채로 만들어졌습니다.`,
    }
  }
  return { outcome: "ok", id: created.value.id, name }
}
