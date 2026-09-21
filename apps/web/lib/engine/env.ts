import "server-only"

/** Where the engine is and what token opens it (3 설계 §3.3).
 *
 * `server-only` is the enforcement, not the comment: importing this from a client component fails the
 * build rather than shipping `ENGINE_API_TOKEN` in the bundle. Neither variable carries `NEXT_PUBLIC_`,
 * so Next never inlines them into client code either.
 *
 * Read at call time rather than at module load so it is testable, and checked again at startup by
 * `instrumentation.ts` so a misconfigured container fails immediately instead of serving 401s that look
 * like the operator's token being wrong.
 */

/** `engine/config.py::MIN_API_TOKEN_LEN`. A shorter value cannot be the engine's token. */
const MIN_TOKEN_LEN = 16

export interface EngineEnv {
  baseUrl: URL
  token: string
}

function required(name: string): string {
  const value = process.env[name]
  if (value === undefined || value === "") {
    throw new Error(`${name}이(가) 설정되지 않았습니다`)
  }
  return value
}

export function engineEnv(): EngineEnv {
  const raw = required("ENGINE_API_URL")
  let baseUrl: URL
  try {
    baseUrl = new URL(raw)
  } catch {
    throw new Error(`ENGINE_API_URL이 URL 형식이 아닙니다: ${raw}`)
  }
  if (baseUrl.pathname !== "/" || baseUrl.search !== "" || baseUrl.hash !== "") {
    // `new URL("/workflows", base)` replaces the base's path, so a configured prefix would vanish and
    // every proxied request would silently go somewhere else.
    throw new Error("ENGINE_API_URL에는 경로·질의·프래그먼트를 붙일 수 없습니다")
  }
  const token = required("ENGINE_API_TOKEN")
  if (token.length < MIN_TOKEN_LEN) {
    throw new Error(`ENGINE_API_TOKEN은 최소 ${MIN_TOKEN_LEN}자여야 합니다`)
  }
  return { baseUrl, token }
}
