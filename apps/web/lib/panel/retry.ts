import type { NodeConfig } from "@/lib/dsl/document"

/** Where the engine overrides the DSL's retry count, so the panel can say so instead of lying.
 *
 * `engine/nodes/http_request.py::policy_for` gives POST and PATCH `maxAttempts=1` whatever the document
 * asks for: they are not idempotent, and a retry can double a payment. A panel that accepted 3 there
 * would show a setting the engine discards.
 */
const NOT_IDEMPOTENT = new Set(["POST", "PATCH"])

export function forcedSingleAttempt(nodeType: string, config: NodeConfig): boolean {
  if (nodeType !== "http_request") return false
  // The engine's own default when the field is absent.
  const method = typeof config.method === "string" ? config.method : "GET"
  return NOT_IDEMPOTENT.has(method)
}
