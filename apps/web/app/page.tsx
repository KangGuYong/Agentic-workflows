import { STATUSES } from "@/lib/design/status"

// Task 1 의 임시 화면: 토큰이 실제로 살아 있는지 눈으로 확인하는 용도다.
// Task 19 가 워크플로 목록으로 이 자리를 대체한다.
export default function Page() {
  return (
    <main className="canvas-grid flex-1 px-8 py-16">
      <p className="instrument-label">000 / SKELETON</p>
      <h1 className="mt-3 text-3xl font-semibold tracking-tight">워크플로 빌더</h1>
      <p className="mt-2 max-w-prose text-fg-muted">
        계기판 방향의 토큰만 놓인 골격입니다. 캔버스와 패널은 이후 Task 에서 올라갑니다.
      </p>

      <ul className="mt-10 flex flex-wrap gap-3">
        {STATUSES.map((status) => (
          <li
            key={status.id}
            className="flex items-center gap-2 border border-ink-600 bg-ink-800 px-3 py-2"
            style={{ borderRadius: "var(--radius)" }}
          >
            <span aria-hidden className="readout" style={{ color: `var(${status.token})` }}>
              {status.glyph}
            </span>
            <span className="text-sm">{status.label}</span>
            <span className="instrument-label">{status.id}</span>
          </li>
        ))}
      </ul>
    </main>
  )
}
