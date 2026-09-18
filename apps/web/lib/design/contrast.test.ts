import { readFileSync } from "node:fs"
import { join } from "node:path"

import { describe, expect, it } from "vitest"

import { contrastRatio, luminance } from "./contrast"

/** WCAG 2.1: 4.5:1 for body-size text, 3:1 for a non-text indicator such as a status glyph. */
const TEXT = 4.5
const GRAPHIC = 3

function theme(name: "dark" | "light"): Record<string, string> {
  const css = readFileSync(join(process.cwd(), "app/globals.css"), "utf8")
  const marker = name === "dark" ? ":root {" : '[data-theme="light"]'
  const start = css.indexOf("{", css.indexOf(marker))
  const body = css.slice(start, css.indexOf("}", start))
  const found: Record<string, string> = {}
  for (const match of body.matchAll(/(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})/g)) {
    found[match[1] as string] = match[2] as string
  }
  // The light theme only overrides; everything else is inherited from :root.
  return name === "dark" ? found : { ...theme("dark"), ...found }
}

describe("contrastRatio", () => {
  it("is 21 for black on white and 1 for a colour on itself", () => {
    expect(contrastRatio("#000000", "#ffffff")).toBeCloseTo(21, 1)
    expect(contrastRatio("#3fb984", "#3fb984")).toBeCloseTo(1, 5)
  })

  it("is symmetric", () => {
    expect(contrastRatio("#0b0f12", "#f5a524")).toBeCloseTo(contrastRatio("#f5a524", "#0b0f12"), 10)
  })

  it("refuses anything that is not #rrggbb", () => {
    expect(() => luminance("#fff")).toThrow(/expected #rrggbb/)
    expect(() => luminance("rebeccapurple")).toThrow(/expected #rrggbb/)
  })
})

describe.each(["dark", "light"] as const)("the %s palette", (name) => {
  const colours = theme(name)
  const text: string[] = ["--fg", "--fg-muted", "--fg-faint"]
  const statuses: string[] = [
    "--st-queued", "--st-running", "--st-succeeded", "--st-failed", "--st-waiting", "--st-default",
  ]

  it.each(text)("reads %s against both surfaces", (token) => {
    for (const surface of ["--ink-900", "--ink-800"]) {
      const ratio = contrastRatio(colours[token] as string, colours[surface] as string)
      expect(ratio, `${token} on ${surface} is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(TEXT)
    }
  })

  it.each(statuses)("shows %s as an indicator against both surfaces", (token) => {
    // Only the glyph carries the colour; the label beside it is always --fg, so the text threshold does
    // not apply here. The glyph still has to be visible, hence 3:1 rather than nothing.
    for (const surface of ["--ink-900", "--ink-800"]) {
      const ratio = contrastRatio(colours[token] as string, colours[surface] as string)
      expect(ratio, `${token} on ${surface} is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(GRAPHIC)
    }
  })

  it("keeps text on the accent fill readable", () => {
    const ratio = contrastRatio(colours["--accent-fg"] as string, colours["--accent"] as string)
    expect(ratio, `--accent-fg on --accent is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(TEXT)
  })
})
