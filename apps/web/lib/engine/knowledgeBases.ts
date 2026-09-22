/** Knowledge bases from the browser, through the BFF proxy (knowledge-base design §6, §7).
 *
 * An upload is the File object itself as the request body: fetch streams it, the proxy streams it on,
 * and nothing here buffers 50 MB into memory. The name rides in the query because the body has no
 * other place for it.
 */

import { messageOf } from "./envelope"

export interface KnowledgeBaseSummary {
  id: string
  name: string
  embedModel: string
  fileCount: number
  createdAt: string
}

export type FileStatus = "pending" | "processing" | "ready" | "failed"

export interface KbFile {
  id: string
  filename: string
  size: number
  status: FileStatus
  error: string | null
  createdAt: string
  updatedAt: string
}

type Failed = { outcome: "failed"; message: string }

export async function listKnowledgeBases(init: { signal?: AbortSignal } = {}): Promise<{ outcome: "ok"; knowledgeBases: KnowledgeBaseSummary[] } | Failed> {
  const response = await fetch("/api/engine/knowledge-bases", { signal: init.signal })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) return { outcome: "failed", message: messageOf(body, `지식베이스 목록을 불러오지 못했습니다 (${response.status})`) }
  const raw = (body as { knowledgeBases?: unknown } | null)?.knowledgeBases
  if (!Array.isArray(raw)) return { outcome: "failed", message: "지식베이스 응답을 이해하지 못했습니다" }
  return { outcome: "ok", knowledgeBases: raw as KnowledgeBaseSummary[] }
}

export async function createKnowledgeBase(name: string): Promise<{ outcome: "ok"; knowledgeBase: KnowledgeBaseSummary } | Failed> {
  const response = await fetch("/api/engine/knowledge-bases", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name }),
  })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) return { outcome: "failed", message: messageOf(body, `지식베이스를 만들지 못했습니다 (${response.status})`) }
  return { outcome: "ok", knowledgeBase: body as KnowledgeBaseSummary }
}

export async function deleteKnowledgeBase(id: string): Promise<{ outcome: "ok" } | Failed> {
  const response = await fetch(`/api/engine/knowledge-bases/${encodeURIComponent(id)}`, { method: "DELETE" })
  if (response.status === 204) return { outcome: "ok" }
  const body: unknown = await response.json().catch(() => null)
  return { outcome: "failed", message: messageOf(body, `지식베이스를 삭제하지 못했습니다 (${response.status})`) }
}

export async function listFiles(kbId: string, init: { signal?: AbortSignal } = {}): Promise<{ outcome: "ok"; files: KbFile[] } | Failed> {
  const response = await fetch(`/api/engine/knowledge-bases/${encodeURIComponent(kbId)}/files`, { signal: init.signal })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) return { outcome: "failed", message: messageOf(body, `파일 목록을 불러오지 못했습니다 (${response.status})`) }
  const raw = (body as { files?: unknown } | null)?.files
  if (!Array.isArray(raw)) return { outcome: "failed", message: "파일 목록 응답을 이해하지 못했습니다" }
  return { outcome: "ok", files: raw as KbFile[] }
}

export async function uploadFile(kbId: string, file: File): Promise<{ outcome: "ok"; file: KbFile } | Failed> {
  const response = await fetch(
    `/api/engine/knowledge-bases/${encodeURIComponent(kbId)}/files?name=${encodeURIComponent(file.name)}`,
    { method: "PUT", headers: { "content-type": file.type || "application/octet-stream" }, body: file },
  )
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) return { outcome: "failed", message: messageOf(body, `파일을 올리지 못했습니다 (${response.status})`) }
  return { outcome: "ok", file: body as KbFile }
}

export async function deleteFile(kbId: string, fileId: string): Promise<{ outcome: "ok" } | Failed> {
  const response = await fetch(
    `/api/engine/knowledge-bases/${encodeURIComponent(kbId)}/files/${encodeURIComponent(fileId)}`,
    { method: "DELETE" },
  )
  if (response.status === 204) return { outcome: "ok" }
  const body: unknown = await response.json().catch(() => null)
  return { outcome: "failed", message: messageOf(body, `파일을 삭제하지 못했습니다 (${response.status})`) }
}
