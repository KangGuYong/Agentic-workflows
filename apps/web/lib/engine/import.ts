import { importedName, readWorkflowFile } from "@/lib/dsl/transfer"

import { createWorkflow, putWorkflow } from "./workflows"

/** Turning a picked file into a workflow (Task 23).
 *
 * This is the **list**'s import: a file becomes a workflow of its own. The editor's import is a
 * different thing -- it places the file's nodes into the document already open (`lib/dsl/insert.ts`)
 * -- and both start from the same `readWorkflowFile`, so a file either screen accepts is a file the
 * other accepts too.
 */

export type ImportOutcome =
  | { outcome: "ok"; id: string; name: string }
  /** The file was refused before anything was created. Nothing exists to clean up. */
  | { outcome: "rejected"; message: string }
  /** The workflow exists but is empty: the create succeeded and the document did not save. */
  | { outcome: "partial"; id: string; name: string; message: string }

export async function importWorkflowFile(file: File): Promise<ImportOutcome> {
  const read = await readWorkflowFile(file)
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

export { readWorkflowFile }
