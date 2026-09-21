/** Fail at startup on a misconfigured container, not on the first request.
 *
 * Without this, a web container started without `ENGINE_API_URL`/`ENGINE_API_TOKEN` looks healthy and
 * every proxied call comes back 401 — which reads as the operator's token being wrong rather than the
 * container never having been given one.
 *
 * Deliberately not run during `next build`: the image is built without secrets, and a build that
 * demanded the production token would have to be given one.
 */
export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME !== "nodejs") return
  if (process.env.NEXT_PHASE === "phase-production-build") return
  const { engineEnv } = await import("@/lib/engine/env")
  engineEnv()
}
