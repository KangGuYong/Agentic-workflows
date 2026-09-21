/** Secret names, checked before the request (3 설계 §7, Task 19).
 *
 * The engine's rule is `^[A-Z][A-Z0-9_]{0,63}$` (`api/routers/secrets.py::NAME`, and the same regex in
 * `secrets/markers.py`). It answers a malformed name with a **404**, deliberately: the path segment is
 * the identity of the resource, and a name that cannot exist is indistinguishable from one that does
 * not. That is right for an API and useless to a person typing a name -- "찾을 수 없습니다" does not say
 * that lowercase is the problem.
 *
 * So the same rule is checked here, and what it says is the reason rather than the verdict.
 */

export const MAX_NAME_LENGTH = 64
export const MIN_VALUE_LENGTH = 8
export const MAX_VALUE_LENGTH = 4096

const VALID = /^[A-Z][A-Z0-9_]{0,63}$/

/** Why this name is not usable, or null when it is. */
export function nameProblem(name: string): string | null {
  if (name === "") return "이름을 입력해 주세요."
  if (name.length > MAX_NAME_LENGTH) return `이름은 ${MAX_NAME_LENGTH}자 이하여야 합니다.`
  if (VALID.test(name)) return null

  // Specific before general: "대문자로 시작해야 합니다" is actionable, "형식이 맞지 않습니다" is not.
  if (/^[0-9_]/.test(name)) return "이름은 영문 대문자로 시작해야 합니다."
  if (/[a-z]/.test(name)) return "이름에는 소문자를 쓸 수 없습니다. 영문 대문자, 숫자, 밑줄만 됩니다."
  return "이름에는 영문 대문자, 숫자, 밑줄(_)만 쓸 수 있습니다."
}

/** Why this value is not usable, or null when it is. */
export function valueProblem(value: string): string | null {
  if (value.length < MIN_VALUE_LENGTH) {
    // The engine's floor exists so that value-based redaction cannot scrub unrelated text; saying the
    // number is more useful than saying why.
    return `값은 ${MIN_VALUE_LENGTH}자 이상이어야 합니다.`
  }
  if (value.length > MAX_VALUE_LENGTH) return `값은 ${MAX_VALUE_LENGTH}자 이하여야 합니다.`
  return null
}

/** The name as it would be if the obvious mistake were corrected, for a "이렇게 쓰시겠어요?" hint. */
export function suggestName(name: string): string | null {
  const upper = name.toUpperCase().replace(/[^A-Z0-9_]/g, "_")
  const trimmed = upper.replace(/^[0-9_]+/, "").slice(0, MAX_NAME_LENGTH)
  // Only when the correction is actually usable and actually different; a suggestion the tenant would
  // have to fix again is noise.
  return trimmed !== name && nameProblem(trimmed) === null ? trimmed : null
}
