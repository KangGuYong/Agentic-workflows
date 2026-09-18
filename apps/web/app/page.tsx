import { Canvas } from "@/components/canvas/Canvas"
import { engineFetch } from "@/lib/engine/client"
import type { NodeType } from "@/lib/palette"

// The editor screen. Task 19 puts a workflow list in front of it; for now it opens on an empty
// document so the canvas can be used and looked at.
export const dynamic = "force-dynamic"

export default async function Page() {
  const { nodeTypes } = await engineFetch<{ nodeTypes: NodeType[] }>("/node-types")
  return <Canvas types={nodeTypes} />
}
