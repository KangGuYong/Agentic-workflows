"use client"

import { useEffect, useState } from "react"

import {
  createKnowledgeBase,
  deleteFile,
  deleteKnowledgeBase,
  listFiles,
  listKnowledgeBases,
  uploadFile,
  type KbFile,
  type KnowledgeBaseSummary,
} from "@/lib/engine/knowledgeBases"

/** Knowledge bases and their files (knowledge-base design §7).
 *
 * Ingestion happens somewhere else, minutes later, so the file table is a status board: it re-reads
 * itself every few seconds only while something is still pending, and stops the moment nothing is.
 */

const POLL_MS = 5_000
const STATUS: Record<KbFile["status"], string> = { pending: "대기 중", processing: "처리 중", ready: "완료", failed: "실패" }

export function KnowledgeBasesScreen({ initial }: { initial: KnowledgeBaseSummary[] }) {
  const [bases, setBases] = useState(initial)
  const [open, setOpen] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function refresh() {
    const result = await listKnowledgeBases()
    if (result.outcome === "ok") setBases(result.knowledgeBases)
    else setError(result.message)
  }

  const current = bases.find((kb) => kb.id === open) ?? null

  return (
    <div className="flex flex-col gap-8">
      <CreateForm onCreated={() => void refresh()} onError={setError} />
      {error === null ? null : (
        <p role="alert" className="border px-3 py-2 text-xs" style={{ borderRadius: "var(--radius)", borderColor: "var(--st-failed)", color: "var(--st-failed)" }}>
          {error}
        </p>
      )}
      <section>
        <h2 className="instrument-label mb-2">지식베이스</h2>
        {bases.length === 0 ? (
          <p className="text-sm text-fg-muted">아직 지식베이스가 없습니다.</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {bases.map((kb) => (
              <li key={kb.id} className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={() => setOpen(kb.id === open ? null : kb.id)}
                  aria-pressed={kb.id === open}
                  className="readout flex-1 border border-ink-600 bg-ink-800 px-3 py-2 text-left text-sm aria-pressed:border-ink-400"
                  style={{ borderRadius: "var(--radius)" }}
                >
                  {kb.name} <span className="text-xs text-fg-faint">파일 {kb.fileCount}개</span>
                </button>
                <button
                  type="button"
                  className="text-xs text-fg-muted underline"
                  onClick={() => {
                    if (!window.confirm(`'${kb.name}' 지식베이스와 모든 파일을 삭제할까요? 이를 쓰는 워크플로는 실행에 실패합니다.`)) return
                    void deleteKnowledgeBase(kb.id).then((result) => {
                      if (result.outcome === "ok") {
                        if (open === kb.id) setOpen(null)
                        void refresh()
                      } else setError(result.message)
                    })
                  }}
                >
                  삭제
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
      {current === null ? null : <Files key={current.id} kb={current} onChanged={() => void refresh()} onError={setError} />}
    </div>
  )
}

function CreateForm({ onCreated, onError }: { onCreated: () => void; onError: (message: string) => void }) {
  const [name, setName] = useState("")
  const [busy, setBusy] = useState(false)
  const ready = name.trim() !== "" && name.trim().length <= 100

  async function create() {
    if (!ready) return
    setBusy(true)
    const result = await createKnowledgeBase(name.trim())
    setBusy(false)
    if (result.outcome === "ok") {
      setName("")
      onCreated()
    } else onError(result.message)
  }

  return (
    <section>
      <h2 className="instrument-label mb-2">지식베이스 만들기</h2>
      <div className="flex items-center gap-2">
        <label htmlFor="kb-name" className="sr-only">새 지식베이스 이름</label>
        <input
          id="kb-name"
          className="flex-1 border border-ink-600 bg-ink-700 px-2 py-1.5 text-sm outline-none focus:border-ink-500"
          style={{ borderRadius: "var(--radius)" }}
          value={name}
          placeholder="예: 제품 매뉴얼"
          onChange={(event) => setName(event.target.value)}
        />
        <button
          type="button"
          onClick={() => void create()}
          disabled={!ready || busy}
          className="border px-3 py-1.5 text-xs disabled:opacity-35"
          style={{ borderRadius: "var(--radius)", borderColor: "var(--accent)", color: "var(--accent)" }}
        >
          {busy ? "만드는 중" : "만들기"}
        </button>
      </div>
    </section>
  )
}

function Files({ kb, onChanged, onError }: { kb: KnowledgeBaseSummary; onChanged: () => void; onError: (message: string) => void }) {
  const [files, setFiles] = useState<KbFile[] | null>(null)
  const [uploading, setUploading] = useState(0)

  async function load() {
    const result = await listFiles(kb.id)
    if (result.outcome === "ok") setFiles(result.files)
    else onError(result.message)
  }

  useEffect(() => {
    let cancelled = false
    // Inlined rather than calling `load()`: an effect that hands off to a named async setState-setter
    // trips `react-hooks/set-state-in-effect`, which can't see the fetch's `await` in between.
    void listFiles(kb.id).then((result) => {
      if (cancelled) return
      if (result.outcome === "ok") setFiles(result.files)
      else onError(result.message)
    })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reload when the open knowledge base changes
  }, [kb.id])

  // Only while something is in flight: an idle table polling forever is load on the engine for nothing.
  const busy = files?.some((file) => file.status === "pending" || file.status === "processing") ?? false
  useEffect(() => {
    if (!busy) return
    const timer = setInterval(() => void load(), POLL_MS)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `load` reads kb.id, which is the other dep
  }, [busy, kb.id])

  async function upload(chosen: FileList | null) {
    if (chosen === null || chosen.length === 0) return
    setUploading(chosen.length)
    for (const file of Array.from(chosen)) {
      const result = await uploadFile(kb.id, file)
      if (result.outcome === "failed") onError(`${file.name}: ${result.message}`)
      setUploading((n) => n - 1)
    }
    await load()
    onChanged()
  }

  return (
    <section>
      <h2 className="instrument-label mb-2">{kb.name}의 파일</h2>
      <div className="mb-3 flex items-center gap-3">
        <label htmlFor="kb-upload" className="border px-3 py-1.5 text-xs" style={{ borderRadius: "var(--radius)", borderColor: "var(--accent)", color: "var(--accent)" }}>
          파일 올리기
        </label>
        <input id="kb-upload" type="file" multiple className="sr-only" onChange={(event) => void upload(event.target.files)} />
        {uploading > 0 ? <span className="text-xs text-fg-muted">올리는 중 ({uploading})</span> : null}
        <span className="text-xs text-fg-faint">PDF·오피스 문서는 MinerU가, .md·.txt는 바로 처리됩니다. 파일당 최대 50MB.</span>
      </div>
      {files === null ? (
        <p className="text-sm text-fg-muted">불러오는 중…</p>
      ) : files.length === 0 ? (
        <p className="text-sm text-fg-muted">아직 파일이 없습니다.</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="rule-engraved text-left">
              <th scope="col" className="instrument-label pb-2 font-normal">이름</th>
              <th scope="col" className="instrument-label pb-2 font-normal">크기</th>
              <th scope="col" className="instrument-label pb-2 font-normal">상태</th>
              <th scope="col" className="sr-only">작업</th>
            </tr>
          </thead>
          <tbody>
            {files.map((file) => (
              <tr key={file.id} className="border-b border-ink-700">
                <td className="readout py-2 pr-4">{file.filename}</td>
                <td className="readout py-2 pr-4 text-xs text-fg-faint">{formatSize(file.size)}</td>
                <td className="py-2 pr-4 text-xs">
                  <span style={{ color: file.status === "failed" ? "var(--st-failed)" : file.status === "ready" ? "var(--st-succeeded)" : "var(--st-waiting)" }}>
                    {STATUS[file.status]}
                  </span>
                  {file.error === null ? null : <span className="ml-2 text-fg-muted">{file.error}</span>}
                </td>
                <td className="py-2 text-right">
                  <button
                    type="button"
                    className="text-xs text-fg-muted underline"
                    onClick={() => {
                      if (!window.confirm(`'${file.filename}'을 지식베이스에서 삭제할까요?`)) return
                      void deleteFile(kb.id, file.id).then((result) => {
                        if (result.outcome === "ok") {
                          void load()
                          onChanged()
                        } else onError(result.message)
                      })
                    }}
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
  )
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
