import { KnowledgeBasesScreen } from "@/components/knowledge/KnowledgeBasesScreen"
import { Shell } from "@/components/shell/Shell"
import { engineFetch } from "@/lib/engine/client"
import type { KnowledgeBaseSummary } from "@/lib/engine/knowledgeBases"

export const dynamic = "force-dynamic"

export default async function Page() {
  let bases: KnowledgeBaseSummary[] = []
  let error: string | null = null
  try {
    bases = (await engineFetch<{ knowledgeBases: KnowledgeBaseSummary[] }>("/knowledge-bases")).knowledgeBases
  } catch {
    error = "지식베이스 목록을 불러오지 못했습니다. 엔진에 연결할 수 없습니다."
  }

  return (
    <Shell title="지식베이스">
      {error === null ? null : (
        <p role="alert" className="mb-4 border px-3 py-2 text-xs" style={{ borderRadius: "var(--radius)", borderColor: "var(--st-failed)", color: "var(--st-failed)" }}>
          {error}
        </p>
      )}
      <KnowledgeBasesScreen initial={bases} />
    </Shell>
  )
}
