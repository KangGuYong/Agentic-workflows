/** Secret management from the browser, through the BFF proxy (Task 19).
 *
 * The API cannot return a value -- `GET /secrets` lists names and timestamps only, and there is no
 * endpoint that reads one back. Nothing here pretends otherwise: there is no `getSecret`, because a
 * function with that name would invite a UI that implies the value could be shown.
 */

import { messageOf } from "./envelope"

export interface SecretSummary {
  name: string
  createdAt: string
  updatedAt: string
}

export type SecretListResult =
  | { outcome: "ok"; secrets: SecretSummary[] }
  | { outcome: "failed"; message: string }

export type SecretResult = { outcome: "ok" } | { outcome: "failed"; message: string }

export async function listSecrets(init: { signal?: AbortSignal } = {}): Promise<SecretListResult> {
  const response = await fetch("/api/engine/secrets", { signal: init.signal })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    return { outcome: "failed", message: messageOf(body, `시크릿 목록을 불러오지 못했습니다 (${response.status})`) }
  }
  const raw = (body as { secrets?: unknown } | null)?.secrets
  if (!Array.isArray(raw)) return { outcome: "failed", message: "시크릿 응답을 이해하지 못했습니다" }
  return {
    outcome: "ok",
    secrets: raw.filter(
      (row): row is SecretSummary =>
        typeof row === "object" && row !== null && typeof (row as SecretSummary).name === "string",
    ),
  }
}

/** Create or replace. The engine has no distinction, and neither should the screen. */
export async function putSecret(name: string, value: string): Promise<SecretResult> {
  const response = await fetch(`/api/engine/secrets/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ value }),
  })
  if (response.status === 204) return { outcome: "ok" }
  const body: unknown = await response.json().catch(() => null)
  return { outcome: "failed", message: messageOf(body, `시크릿을 저장하지 못했습니다 (${response.status})`) }
}

export async function deleteSecret(name: string): Promise<SecretResult> {
  const response = await fetch(`/api/engine/secrets/${encodeURIComponent(name)}`, { method: "DELETE" })
  if (response.status === 204) return { outcome: "ok" }
  const body: unknown = await response.json().catch(() => null)
  return { outcome: "failed", message: messageOf(body, `시크릿을 삭제하지 못했습니다 (${response.status})`) }
}
