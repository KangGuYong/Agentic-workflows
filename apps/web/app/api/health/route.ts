import { NextResponse } from "next/server"

/** Liveness, and the target `http_request` calls in the end-to-end tests (Task 20, Task 21).
 *
 * **Deliberately does not touch the engine.** A healthcheck that did would report this container as
 * unhealthy whenever the engine was down, and Docker would restart a process that is working perfectly
 * -- while the thing that is actually broken keeps running. This answers one question: is Next serving?
 *
 * No token either, for the same reason: a liveness probe that can fail on a configuration mistake is a
 * liveness probe that lies.
 */
export const dynamic = "force-dynamic"

export function GET() {
  return NextResponse.json(
    { status: "ok" },
    // A cached liveness answer is worse than none: it would keep saying "ok" after the process stopped
    // being able to produce one.
    { headers: { "cache-control": "no-store" } },
  )
}

/** The E2E workflow POSTs here through `http_request`, and asserts on what comes back.
 *
 * Echoing the body is the point: it is how a test proves the engine rendered a template into a real
 * request and read a real response back. The body is bounded by the engine's own request limits.
 */
export async function POST(request: Request) {
  const body: unknown = await request.json().catch(() => null)
  return NextResponse.json({ status: "ok", echo: body }, { headers: { "cache-control": "no-store" } })
}
