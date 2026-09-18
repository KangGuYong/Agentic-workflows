import "server-only"

import { engineEnv } from "./env"

/** Server-side engine calls for React Server Components (3 설계 §3.3).
 *
 * The BFF proxy is for the *browser*; a server component is already on the server and has no reason to
 * take a network hop through its own route handler. Both read the token from the same place, and
 * `server-only` keeps this module out of any client bundle.
 */
export async function engineFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const { baseUrl, token } = engineEnv()
  const response = await fetch(new URL(path, baseUrl), {
    ...init,
    headers: { ...init.headers, authorization: `Bearer ${token}`, accept: "application/json" },
    cache: "no-store",
  })
  if (!response.ok) {
    // The body may carry the engine's error envelope, but a server component cannot render it usefully
    // -- it is a crash, not a form error. Status and path are what a log needs; never the query string
    // (MVP 설계 10.1).
    throw new Error(`엔진 요청 실패: ${init.method ?? "GET"} ${path} -> ${response.status}`)
  }
  return (await response.json()) as T
}
