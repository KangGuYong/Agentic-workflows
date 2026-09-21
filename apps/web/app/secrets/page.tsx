import { SecretsScreen } from "@/components/secrets/SecretsScreen"
import { Shell } from "@/components/shell/Shell"
import { engineFetch } from "@/lib/engine/client"
import type { SecretSummary } from "@/lib/engine/secrets"

/** Secrets (Task 19).
 *
 * Names and timestamps only, because that is all the API has. There is no endpoint that reads a value
 * back, and this screen is built so nothing on it suggests there could be.
 */
export const dynamic = "force-dynamic"

export default async function Page() {
  let secrets: SecretSummary[] = []
  let error: string | null = null
  try {
    const result = await engineFetch<{ secrets: SecretSummary[] }>("/secrets")
    secrets = result.secrets
  } catch {
    error = "시크릿 목록을 불러오지 못했습니다. 엔진에 연결할 수 없습니다."
  }

  return (
    <Shell title="시크릿">
      {error === null ? null : (
        <p role="alert" className="mb-4 border px-3 py-2 text-xs" style={{ borderRadius: "var(--radius)", borderColor: "var(--st-failed)", color: "var(--st-failed)" }}>
          {error}
        </p>
      )}
      <SecretsScreen initial={secrets} />
    </Shell>
  )
}
