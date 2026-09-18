import { describe, expect, it } from "vitest"

import type { EditorNode } from "./document"
import { NODE_SIZE } from "./layout"
import { anyNodeVisible } from "./viewport"

function node(id: string, x: number, y: number): EditorNode {
  return { id, type: "llm", position: { x, y } }
}

/** A 1000x600 canvas looking at flow coordinates (0,0)-(1000,600) at zoom 1. */
const VIEW = { viewport: { x: 0, y: 0, zoom: 1 }, width: 1000, height: 600 }

describe("anyNodeVisible", () => {
  it("is true when a node sits inside the view", () => {
    expect(anyNodeVisible([node("a", 100, 100)], VIEW)).toBe(true)
  })

  it("is true when a node only overlaps the edge of the view", () => {
    // Half on screen is still on screen; fitting the view would be a jarring jump for no reason.
    expect(anyNodeVisible([node("a", -NODE_SIZE.width / 2, 100)], VIEW)).toBe(true)
    expect(anyNodeVisible([node("a", 960, 560)], VIEW)).toBe(true)
  })

  it("is false when every node is off screen", () => {
    expect(anyNodeVisible([node("a", 5000, 5000), node("b", -5000, -5000)], VIEW)).toBe(false)
  })

  it("is false for a node just past each edge", () => {
    expect(anyNodeVisible([node("a", -NODE_SIZE.width - 1, 100)], VIEW)).toBe(false)
    expect(anyNodeVisible([node("a", 1001, 100)], VIEW)).toBe(false)
    expect(anyNodeVisible([node("a", 100, -NODE_SIZE.height - 1)], VIEW)).toBe(false)
    expect(anyNodeVisible([node("a", 100, 601)], VIEW)).toBe(false)
  })

  it("accounts for pan", () => {
    // Panning right by 900px moves flow x=1000 into view and flow x=0 out of it.
    const panned = { ...VIEW, viewport: { x: -900, y: 0, zoom: 1 } }
    expect(anyNodeVisible([node("a", 1000, 100)], panned)).toBe(true)
    expect(anyNodeVisible([node("a", 0, 100)], panned)).toBe(false)
  })

  it("accounts for zoom", () => {
    // At zoom 0.5 the same canvas shows twice as much flow space.
    const zoomedOut = { ...VIEW, viewport: { x: 0, y: 0, zoom: 0.5 } }
    expect(anyNodeVisible([node("a", 1800, 100)], zoomedOut)).toBe(true)
    expect(anyNodeVisible([node("a", 1800, 100)], VIEW)).toBe(false)
  })

  it("is true for an empty document, so an empty canvas is never refitted", () => {
    // Nothing to look at means nothing is wrong; fitting an empty graph would reset the user's pan for
    // no reason every time they cleared the canvas.
    expect(anyNodeVisible([], VIEW)).toBe(true)
  })

  it("is true when the container has not been measured yet", () => {
    // On the first render React Flow reports 0x0. Treating that as "nothing visible" would fire a fit
    // on mount and fight the component's own `fitView`.
    expect(anyNodeVisible([node("a", 100, 100)], { ...VIEW, width: 0, height: 0 })).toBe(true)
  })
})
