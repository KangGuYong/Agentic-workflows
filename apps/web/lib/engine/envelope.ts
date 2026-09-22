/** Reading the engine's error envelope, `{ error: { code, message, details } }`.
 *
 * Every client in this folder answers a failed request with the engine's own message when it sent one
 * and a local fallback when it did not -- an empty string counts as not sending one.
 */

interface Envelope {
  error?: { code?: unknown; message?: unknown; details?: unknown }
}

export function messageOf(body: unknown, fallback: string): string {
  const message = (body as Envelope | null)?.error?.message
  return typeof message === "string" && message !== "" ? message : fallback
}

export function detailsOf(body: unknown): Record<string, unknown> | null {
  const details = (body as Envelope | null)?.error?.details
  return typeof details === "object" && details !== null ? (details as Record<string, unknown>) : null
}
