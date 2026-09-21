/** Timestamps a person reads (Task 19).
 *
 * Client-side only by nature: the server does not know the reader's timezone, so formatting one there
 * produces a string the browser then disagrees with. Callers render it through `useSyncExternalStore`
 * with an empty server snapshot rather than an effect.
 */
export function localTime(at: string | undefined): string {
  if (at === undefined || at === "") return ""
  const parsed = new Date(at)
  // An unparseable timestamp is not worth a row of "Invalid Date"; the rest of the row still reads.
  return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleString("ko-KR")
}

/** How long ago, for a list where the exact minute matters less than the order. */
export function relativeTime(at: string | undefined, now: number): string {
  if (at === undefined || at === "") return ""
  const parsed = Date.parse(at)
  if (!Number.isFinite(parsed)) return ""

  // Negative when the two clocks disagree, which the `< 60` branch answers for anyway -- no clamp.
  const seconds = Math.round((now - parsed) / 1000)
  if (seconds < 60) return "방금"
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}분 전`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}시간 전`
  return `${Math.floor(hours / 24)}일 전`
}
