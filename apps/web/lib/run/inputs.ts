import type { EditorDsl } from "@/lib/dsl/document"
import type { JsonSchema } from "@/lib/template/schema"

/** The form a run is started from (3 설계 §8, Task 14).
 *
 * The `start` node's `inputs` config **is** the schema: whatever the tenant declared there is what the
 * run is given. So the dialog is RJSF over that schema, exactly like the settings panel is RJSF over a
 * node type's `configSchema`.
 *
 * The plan said to reuse Task 11's renderer "in read-and-fill mode". That renderer edits a *schema*;
 * filling *values* against one is a different job and is what RJSF already does. Reusing the schema
 * editor here would have meant writing a second form engine.
 */

/** The engine reserves the id `start` (`RESERVED_NODE_ID`), so there is exactly one and it is named. */
export const START_NODE_ID = "start"

/** The schema a run's inputs are filled against, or `null` when the workflow declares none. */
export function inputSchema(dsl: EditorDsl): JsonSchema | null {
  const start = dsl.nodes.find((node) => node.id === START_NODE_ID)
  const inputs = start?.config?.["inputs"]
  if (typeof inputs !== "object" || inputs === null || Array.isArray(inputs)) return null
  const schema = inputs as JsonSchema
  // `{}` means "declared nothing", which is the same as declaring none: there is no form to draw.
  return Object.keys(schema).length === 0 ? null : schema
}

/** Whether the run dialog has anything to ask for. */
export function needsInputs(dsl: EditorDsl): boolean {
  const schema = inputSchema(dsl)
  if (schema === null) return false
  const properties = schema["properties"]
  return typeof properties === "object" && properties !== null && Object.keys(properties).length > 0
}
