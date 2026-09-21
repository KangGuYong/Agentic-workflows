import Link from "next/link"

/** The chrome every screen outside the canvas sits in (Task 19, 시각 디자인).
 *
 * The canvas has no chrome on purpose -- it is an instrument face, edge to edge. Everything else is a
 * panel *behind* it: a hairline-ruled header with the product name etched in mono, and the one amber
 * signal reserved for "this is live" rather than spent on decoration.
 */
export function Shell({ title, action, children }: { title: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="panel-surface min-h-dvh">
      <header className="rule-engraved bg-ink-800">
        <div className="mx-auto flex max-w-4xl items-center gap-4 px-6 py-3">
          <Link href="/" className="flex items-center gap-2">
            {/* The one amber signal the direction allows, spent on what it is for: this thing is on. */}
            <span
              aria-hidden
              className="signal-live size-1.5 rounded-full"
              style={{ background: "var(--accent)" }}
            />
            <span className="instrument-label tracking-[0.2em] text-fg-muted hover:text-fg">
              AGENTIC WORKFLOWS
            </span>
          </Link>
          <nav className="ml-auto flex items-center gap-4 text-xs">
            <Link href="/" className="text-fg-muted hover:text-fg">
              워크플로
            </Link>
            <Link href="/secrets" className="text-fg-muted hover:text-fg">
              시크릿
            </Link>
          </nav>
        </div>
      </header>

      <main className="panel-enter mx-auto max-w-4xl px-6 py-8">
        <div className="mb-6 flex items-center gap-4">
          <h1 className="text-lg font-semibold">{title}</h1>
          {action === undefined ? null : <div className="ml-auto">{action}</div>}
        </div>
        {children}
      </main>
    </div>
  )
}
