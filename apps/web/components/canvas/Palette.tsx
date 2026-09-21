"use client"

import { groupByCategory, type NodeType } from "@/lib/palette"

/** Korean headings for the engine's category names. The engine has no display strings for these, so
 * the table lives here; `기타` is supplied by `groupByCategory` itself. */
const HEADING: Record<string, string> = {
  IO: "입출력",
  AI: "모델",
  Logic: "흐름",
  Action: "동작",
  Human: "사람",
}

export const DRAG_TYPE = "application/x-workflow-node-type"

export function Palette({ types }: { types: NodeType[] }) {
  return (
    <aside aria-label="노드 팔레트" className="w-52 shrink-0 overflow-y-auto border-r border-ink-600 bg-ink-800">
      <p className="instrument-label border-b border-ink-600 px-4 py-3">PALETTE</p>
      {groupByCategory(types).map((group) => (
        <section key={group.category} className="border-b border-ink-600 px-4 py-3">
          <h2 className="instrument-label mb-2">{HEADING[group.category] ?? group.category}</h2>
          <ul className="flex flex-col gap-1">
            {group.types.map((item) => (
              <li key={item.type}>
                <button
                  type="button"
                  draggable
                  onDragStart={(event) => {
                    event.dataTransfer.setData(DRAG_TYPE, item.type)
                    event.dataTransfer.effectAllowed = "copy"
                  }}
                  className="w-full cursor-grab border border-transparent bg-ink-700 px-2 py-1.5 text-left text-sm hover:border-ink-500 active:cursor-grabbing"
                  style={{ borderRadius: "var(--radius)" }}
                >
                  {item.label}
                  <span className="instrument-label ml-2">{item.type}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </aside>
  )
}
