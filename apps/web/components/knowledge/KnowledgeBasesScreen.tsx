"use client"

import { useEffect, useState } from "react"

import { ImportButton } from "@/components/transfer/ImportButton"
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
const MAX_FILE_BYTES = 50_000_000 // the engine default (KB_MAX_FILE_BYTES); both sides say "50MB"
const STATUS: Record<KbFile["status"], string> = { pending: "대기 중", processing: "처리 중", ready: "완료", failed: "실패" }

export function KnowledgeBasesScreen({ initial }: { initial: KnowledgeBaseSummary[] }) {
  const [bases, setBases] = useState(initial)
  const [open, setOpen] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function refresh() {
    const result = await listKnowledgeBases()
    if (result.outcome === "ok") {
      setError(null)
      setBases(result.knowledgeBases)
    } else setError(result.message)
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
                  className="readout flex-1 border bg-ink-800 px-3 py-2 text-left text-sm"
                  style={{ borderRadius: "var(--radius)", borderColor: kb.id === open ? "var(--accent)" : "var(--ink-600)" }}
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

function Files({ kb, onChanged, onError }: { kb: KnowledgeBaseSummary; onChanged: () => void; onError: (message: string | null) => void }) {
  const [files, setFiles] = useState<KbFile[] | null>(null)
  const [uploading, setUploading] = useState(0)

  // `clearError` is false right after an upload failure: that error names *this* file, and a routine
  // list refresh succeeding a moment later must not blank it out.
  async function load(clearError = true) {
    const result = await listFiles(kb.id)
    if (result.outcome === "ok") {
      if (clearError) onError(null)
      setFiles(result.files)
    } else onError(result.message)
  }

  useEffect(() => {
    let cancelled = false
    // Inlined rather than calling `load()`: an effect that hands off to a named async setState-setter
    // trips `react-hooks/set-state-in-effect`, which can't see the fetch's `await` in between.
    void listFiles(kb.id).then((result) => {
      if (cancelled) return
      if (result.outcome === "ok") {
        onError(null)
        setFiles(result.files)
      } else onError(result.message)
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
    // `false`: a background refresh is not a user action and must not clear a message nobody has
    // acknowledged, such as the failure of the upload that made this table worth polling.
    const timer = setInterval(() => void load(false), POLL_MS)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `load` reads kb.id, which is the other dep
  }, [busy, kb.id])

  async function uploadOne(file: File) {
    if (file.size > MAX_FILE_BYTES) {
      onError(`${file.name}: 파일당 최대 50MB입니다`)
      return
    }
    setUploading((n) => n + 1)
    const result = await uploadFile(kb.id, file)
    const failed = result.outcome === "failed"
    if (failed) onError(`${file.name}: ${result.message}`)
    setUploading((n) => n - 1)
    await load(!failed)
    // Skipped on failure: `onChanged` refreshes the base list, and that refresh clears the error on
    // success -- which would blank out the message this upload just set.
    if (!failed) onChanged()
  }

  return (
    <section>
      <h2 className="instrument-label mb-2">{kb.name}의 파일</h2>
      <div className="mb-3 flex items-center gap-3">
        <ImportButton
          multiple
          accept=""
          busy={uploading > 0}
          label="파일 올리기"
          inputLabel="파일 올리기"
          title="PDF·오피스 문서는 MinerU가, .md·.txt는 바로 처리됩니다. 파일당 최대 50MB."
          className="border px-3 py-1.5 text-xs disabled:opacity-35"
          style={{ borderRadius: "var(--radius)", borderColor: "var(--accent)", color: "var(--accent)" }}
          // ponytail: one PUT and one list reload per picked file, in parallel; batch them if picks of dozens of files show up.
          onPick={(file) => void uploadOne(file)}
        />
        {uploading > 0 ? (
          <span role="status" className="text-xs text-fg-muted">
            올리는 중 ({uploading})
          </span>
        ) : null}
        <span className="text-xs text-fg-faint">PDF·오피스 문서는 MinerU가, .md·.txt는 바로 처리됩니다. 파일당 최대 50MB.</span>
      </div>
      {files === null ? (
        <p role="status" className="text-sm text-fg-muted">불러오는 중…</p>
      ) : files.length === 0 ? (
        <p className="text-sm text-fg-muted">아직 파일이 없습니다.</p>
      ) : (
        <>
          <p role="status" className="sr-only">
            {busy ? `${files.filter((file) => file.status === "pending" || file.status === "processing").length}개 처리 중` : "처리 중인 파일 없음"}
          </p>
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
        </>
      )}
    </section>
  )
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
