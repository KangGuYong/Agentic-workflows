import "server-only"

import { engineEnv } from "@/lib/engine/env"
import { engineTarget } from "@/lib/engine/paths"

/** The BFF proxy: the only place `ENGINE_API_TOKEN` is used (3 설계 §3.2).
 *
 * The browser cannot call the engine directly. Plan 2b's `TokenAuthMiddleware` requires a bearer token on
 * every path, the engine has no CORS middleware, and -- decisively -- `EventSource` cannot set a request
 * header at all, so subscribing to a run's SSE stream from the browser is not merely awkward but
 * impossible. Putting the token in a query string instead would write it into proxy logs and browser
 * history, against MVP 설계 10.1 (URLs in logs are host + path only).
 *
 * Nothing here interprets a response. The status and body are whatever the engine said, so the editor
 * can read `details.currentRevision` out of a 409 envelope, and a `text/event-stream` body is handed
 * through as a stream rather than buffered.
 */

export const runtime = "nodejs"
export const dynamic = "force-dynamic"

/**
 * Request headers that may reach the engine.
 *
 * Deliberately a short allowlist rather than "everything except a deny list". Never forwarded, and why:
 * `authorization` (a caller must not be able to replace our token), `cookie` (the engine has no session
 * concept, so it would only leak whatever the browser holds for this origin), `host` and `connection`
 * (hop-by-hop, and fetch sets `host` from the target), `content-length` (recomputed for the body we
 * actually send), and the `x-forwarded-*` family (the engine does not consume them, so passing them is
 * only an injection surface).
 */
const FORWARD_REQUEST = ["content-type", "accept", "accept-language", "last-event-id", "idempotency-key"]

/**
 * Response headers that may reach the browser.
 *
 * `content-encoding` and `content-length` are deliberately absent: fetch has already decoded the body,
 * so re-advertising the original encoding or length would describe bytes we are not sending.
 */
const FORWARD_RESPONSE = ["content-type", "cache-control"]

function requestHeaders(request: Request, token: string): Headers {
  const headers = new Headers()
  for (const name of FORWARD_REQUEST) {
    const value = request.headers.get(name)
    if (value !== null) headers.set(name, value)
  }
  headers.set("authorization", `Bearer ${token}`)
  return headers
}

function responseHeaders(upstream: Response): Headers {
  const headers = new Headers()
  for (const name of FORWARD_RESPONSE) {
    const value = upstream.headers.get(name)
    if (value !== null) headers.set(name, value)
  }
  // An SSE response must not be buffered by anything between here and the browser.
  if (headers.get("content-type")?.startsWith("text/event-stream") === true) {
    headers.set("cache-control", "no-cache")
    headers.set("x-accel-buffering", "no")
  }
  return headers
}

type Context = { params: Promise<{ path: string[] }> }

async function proxy(request: Request, context: Context): Promise<Response> {
  const { path } = await context.params
  const target = engineTarget(path)
  // 404 rather than 403: telling a caller a path exists but is forbidden maps out the engine for them.
  if (target === null) return new Response(null, { status: 404 })

  const { baseUrl, token } = engineEnv()
  const url = new URL(target, baseUrl)
  url.search = new URL(request.url).search

  const hasBody = request.method !== "GET" && request.method !== "HEAD"
  const upstream = await fetch(url, {
    method: request.method,
    headers: requestHeaders(request, token),
    body: hasBody ? request.body : null,
    // Required by undici whenever the body is a stream, and still missing from the DOM types.
    ...(hasBody ? ({ duplex: "half" } as Record<string, unknown>) : {}),
    // Task 0 measured what happens without this: the upstream stream survives the client's disconnect,
    // so a closed tab holds an engine SSE connection -- and its Redis pubsub connection -- until the run
    // itself ends, which for a `waiting` run is up to the 30-day limit.
    signal: request.signal,
    // Following a redirect would let it move the request off the path the allowlist admitted.
    redirect: "manual",
  })

  return new Response(upstream.body, { status: upstream.status, headers: responseHeaders(upstream) })
}

export const GET = proxy
export const POST = proxy
export const PUT = proxy
export const PATCH = proxy
export const DELETE = proxy
