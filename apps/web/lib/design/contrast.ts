/** WCAG 2.1 relative luminance and contrast ratio.
 *
 * Here because the palette is checked in CI (`contrast.test.ts`) rather than eyeballed: a tool UI is
 * mostly 11-13px text on coloured chrome, which is exactly where a good-looking palette quietly stops
 * being readable. Tasks that add a colour add it to the table in the test.
 */
const CHANNEL = /^#([0-9a-fA-F]{6})$/

function channel(value: number): number {
  const c = value / 255
  return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)
}

export function luminance(hex: string): number {
  const match = CHANNEL.exec(hex)
  if (match === null) throw new Error(`expected #rrggbb, got: ${hex}`)
  const digits = match[1] as string
  const [r, g, b] = [0, 2, 4].map((at) => channel(parseInt(digits.slice(at, at + 2), 16))) as [number, number, number]
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

export function contrastRatio(foreground: string, background: string): number {
  const a = luminance(foreground)
  const b = luminance(background)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}
