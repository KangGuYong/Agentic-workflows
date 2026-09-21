"use client"

import { useState, useSyncExternalStore } from "react"

import { deleteSecret, listSecrets, putSecret, type SecretSummary } from "@/lib/engine/secrets"
import { localTime } from "@/lib/format/time"
import { nameProblem, suggestName, valueProblem } from "@/lib/secrets/name"

/** Secrets (Task 19).
 *
 * **The value is write-only.** `GET /secrets` returns names and timestamps; there is no endpoint that
 * reads a value back, and there never will be -- that is the point of storing them encrypted. So this
 * screen must not imply otherwise: no masked field showing `••••••` where a value would be (that reads
 * as "it is here, just hidden"), no "보기" control, no placeholder pretending to hold something.
 *
 * What it shows instead is the name, when it was set, and a box that is empty because **this is where
 * you put a new value**, not where the old one lives.
 */
export function SecretsScreen({ initial }: { initial: SecretSummary[] }) {
  const [secrets, setSecrets] = useState(initial)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)

  async function refresh() {
    const result = await listSecrets()
    if (result.outcome === "ok") setSecrets(result.secrets)
    else setError(result.message)
  }

  return (
    <div className="flex flex-col gap-8">
      <SecretForm
        existing={secrets.map((secret) => secret.name)}
        onSaved={(name) => {
          setSaved(name)
          setError(null)
          void refresh()
        }}
        onError={(message) => {
          setError(message)
          setSaved(null)
        }}
      />

      {error === null ? null : (
        <p role="alert" className="border px-3 py-2 text-xs" style={{ borderRadius: "var(--radius)", borderColor: "var(--st-failed)", color: "var(--st-failed)" }}>
          {error}
        </p>
      )}
      {saved === null ? null : (
        <p role="status" className="text-xs" style={{ color: "var(--st-succeeded)" }}>
          {saved} 값을 저장했습니다. 저장된 값은 다시 볼 수 없습니다.
        </p>
      )}

      <section>
        <h2 className="instrument-label mb-2">저장된 시크릿</h2>
        {secrets.length === 0 ? (
          <p className="text-sm text-fg-muted">아직 시크릿이 없습니다.</p>
        ) : (
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="rule-engraved text-left">
                <th scope="col" className="instrument-label pb-2 font-normal">이름</th>
                <th scope="col" className="instrument-label pb-2 font-normal">마지막 변경</th>
                <th scope="col" className="sr-only">작업</th>
              </tr>
            </thead>
            <tbody>
              {secrets.map((secret, index) => (
                <tr key={secret.name} className="panel-enter border-b border-ink-700" style={{ "--row": index } as React.CSSProperties}>
                  <td className="readout py-2 pr-4">{secret.name}</td>
                  <td className="readout py-2 pr-4 text-xs text-fg-faint">
                    <ClientTime at={secret.updatedAt} />
                  </td>
                  <td className="py-2 text-right">
                    <button
                      type="button"
                      onClick={() => {
                        if (!window.confirm(`'${secret.name}' 시크릿을 삭제할까요? 이 값을 쓰는 워크플로는 실행에 실패합니다.`)) return
                        void deleteSecret(secret.name).then((result) => {
                          if (result.outcome === "ok") void refresh()
                          else setError(result.message)
                        })
                      }}
                      className="text-xs text-fg-muted underline"
                    >
                      삭제
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}

function SecretForm({
  existing,
  onSaved,
  onError,
}: {
  existing: readonly string[]
  onSaved: (name: string) => void
  onError: (message: string) => void
}) {
  const [name, setName] = useState("")
  const [value, setValue] = useState("")
  const [busy, setBusy] = useState(false)

  // Checked here as well as by the engine, because the engine answers a malformed name with a 404 --
  // right for an API, useless to someone typing.
  const problem = name === "" ? null : nameProblem(name)
  const suggestion = problem === null ? null : suggestName(name)
  const valueIssue = value === "" ? null : valueProblem(value)
  const replacing = existing.includes(name)
  const ready = name !== "" && value !== "" && problem === null && valueIssue === null

  async function save() {
    if (!ready) return
    setBusy(true)
    const result = await putSecret(name, value)
    setBusy(false)
    if (result.outcome === "ok") {
      onSaved(name)
      setName("")
      // Cleared immediately: leaving a secret in a form field keeps it in the DOM, in the browser's
      // autofill and in any screenshot taken after.
      setValue("")
    } else onError(result.message)
  }

  return (
    <section>
      <h2 className="instrument-label mb-2">시크릿 추가 또는 교체</h2>
      <div className="flex flex-col gap-3">
        {/* The help sits outside the `<label>` and is linked with `aria-describedby`. Inside it, the
            control's accessible name becomes the label and the whole sentence run together, so a
            screen reader reads the explanation on every focus -- and `getByLabelText("값")` cannot find
            the field, which is how this surfaced here exactly as it did in Task 9. */}
        <div>
          <label htmlFor="secret-name" className="instrument-label mb-1 block">
            이름
          </label>
          <input
            id="secret-name"
            aria-describedby="secret-name-help"
            className="w-full border border-ink-600 bg-ink-700 px-2 py-1.5 font-mono text-sm outline-none focus:border-ink-500"
            style={{ borderRadius: "var(--radius)" }}
            value={name}
            aria-invalid={problem !== null}
            onChange={(event) => setName(event.target.value)}
            placeholder="API_KEY"
          />
          {problem === null ? (
            <p id="secret-name-help" className="mt-1 text-xs text-fg-faint">
              영문 대문자로 시작하고, 대문자·숫자·밑줄만 씁니다.
            </p>
          ) : (
            <p id="secret-name-help" className="mt-1 text-xs text-st-failed">
              {problem}
              {suggestion === null ? null : (
                <button type="button" onClick={() => setName(suggestion)} className="ml-2 underline">
                  {suggestion}(으)로 고치기
                </button>
              )}
            </p>
          )}
        </div>

        <div>
          <label htmlFor="secret-value" className="instrument-label mb-1 block">
            값
          </label>
          <input
            id="secret-value"
            type="password"
            autoComplete="off"
            aria-describedby="secret-value-help"
            className="w-full border border-ink-600 bg-ink-700 px-2 py-1.5 font-mono text-sm outline-none focus:border-ink-500"
            style={{ borderRadius: "var(--radius)" }}
            value={value}
            aria-invalid={valueIssue !== null}
            onChange={(event) => setValue(event.target.value)}
          />
          <p id="secret-value-help" className="mt-1 text-xs text-fg-faint">
            {valueIssue ?? "저장하면 암호화되어 보관되고, 이후에는 다시 볼 수 없습니다."}
          </p>
        </div>

        <button
          type="button"
          onClick={() => void save()}
          disabled={!ready || busy}
          className="self-start border px-3 py-1.5 text-xs disabled:opacity-35"
          style={{ borderRadius: "var(--radius)", borderColor: "var(--accent)", color: "var(--accent)" }}
        >
          {busy ? "저장 중" : replacing ? "값 교체" : "저장"}
        </button>
        {replacing && !busy ? (
          <p className="text-xs" style={{ color: "var(--st-waiting)" }}>
            같은 이름의 시크릿이 이미 있습니다. 저장하면 이전 값을 덮어씁니다.
          </p>
        ) : null}
      </div>
    </section>
  )
}

function ClientTime({ at }: { at: string }) {
  return <span>{useSyncExternalStore(() => () => {}, () => localTime(at), () => "")}</span>
}
