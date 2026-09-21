import { readdirSync, readFileSync, statSync } from "node:fs"
import { join } from "node:path"

import { describe, expect, it } from "vitest"

import { STATUSES, statusById, statusOfNodeRun, type StatusId } from "./status"

const IDS: StatusId[] = ["queued", "running", "succeeded", "failed", "waiting", "default"]

describe("run statuses", () => {
  it("covers every state a node run can be in", () => {
    expect(STATUSES.map((status) => status.id)).toEqual(IDS)
  })

  it("names a token that exists in the stylesheet", () => {
    const css = readFileSync(join(process.cwd(), "app/globals.css"), "utf8")
    for (const status of STATUSES) {
      expect(status.token).toBe(`--st-${status.id}`)
      expect(css).toContain(`${status.token}:`)
    }
  })

  it("distinguishes every status by shape as well as colour", () => {
    // Colour alone fails for a red/green deficiency and on a washed-out monitor, and the amber/crimson
    // pair is the one most at risk at badge size. A duplicate glyph would silently undo that.
    const glyphs = STATUSES.map((status) => status.glyph)
    expect(new Set(glyphs).size).toBe(glyphs.length)
  })

  it("has a Korean label for every status", () => {
    for (const status of STATUSES) {
      expect(status.label).toMatch(/[가-힣]/)
    }
  })

  it("refuses an id it does not know", () => {
    expect(() => statusById("nope" as StatusId)).toThrow(/unknown status/)
  })
})

/** Walk the source tree, skipping build output and dependencies. */
function sources(dir: string): string[] {
  const skip = new Set(["node_modules", ".next", "public", "e2e"])
  const found: string[] = []
  for (const entry of readdirSync(dir)) {
    if (skip.has(entry) || entry.startsWith(".")) continue
    const path = join(dir, entry)
    if (statSync(path).isDirectory()) found.push(...sources(path))
    else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) found.push(path)
  }
  return found
}

describe("design tokens", () => {
  it("keeps every colour in globals.css", () => {
    // The aesthetic direction only survives 20 more tasks if there is one place to change a colour.
    // A hex literal in a component is how a design system quietly stops being one.
    const offenders: string[] = []
    for (const path of sources(process.cwd())) {
      const text = readFileSync(path, "utf8")
      for (const [index, line] of text.split("\n").entries()) {
        if (/#[0-9a-fA-F]{3,8}\b/.test(line)) offenders.push(`${path}:${index + 1}: ${line.trim()}`)
      }
    }
    expect(offenders).toEqual([])
  })
})

describe("statusOfNodeRun", () => {
  it("maps each stream status onto a design state", () => {
    // Every value the reducer can produce has to land on one of the six, or `statusById` throws at
    // render time -- on the canvas, during a run.
    for (const status of ["running", "succeeded", "failed", "waiting"] as const) {
      expect(() => statusById(statusOfNodeRun(status))).not.toThrow()
    }
  })

  it("renames `defaulted` to the design's `default`", () => {
    // The engine's word for "finished on its defaultOutput" and the design's sixth state are the same
    // thing named twice. Anywhere else knowing that would be a second place to keep in step.
    expect(statusOfNodeRun("defaulted")).toBe("default")
    expect(statusById(statusOfNodeRun("defaulted")).label).toBe("기본값")
  })

  it("leaves the statuses whose names already match", () => {
    expect(statusOfNodeRun("running")).toBe("running")
    expect(statusOfNodeRun("waiting")).toBe("waiting")
  })
})
