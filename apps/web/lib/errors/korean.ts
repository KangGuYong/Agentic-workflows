/** Turning pydantic's English into Korean (3 설계 §4, Task 13).
 *
 * Most of what `/validate` reports is already Korean: the engine writes its own messages. The exception
 * is a config or policy that fails pydantic, where the engine passes pydantic's own sentence through as
 * `설정 오류: String should have at least 1 character`. That half is English, and this translates it.
 *
 * **It matches on the message text, because the message is all that crosses the wire.** The plan said to
 * key on pydantic's error *type* (`string_too_short`, `literal_error`, …), but `pydantic_issues` in
 * `engine/validator/structure.py` sends only `err["msg"]`. Matching text is fragile in one specific way
 * -- a pydantic upgrade could reword a sentence -- so the exact strings are pinned by a test **in the
 * engine's suite**, where pydantic actually lives and an upgrade would break it loudly. Here the
 * failure mode is safe on its own: an unmatched sentence is shown as-is, in English, which is worse to
 * read but never wrong.
 */

interface Rule {
  match: RegExp
  /** Captures become `$1`, `$2`. A function gets them instead, for a capture that needs its own work. */
  korean: string | ((...captures: string[]) => string)
}

/** Pydantic writes an enumeration as `'a', 'b' or 'c'`. Left alone it produces
 * `'a' or 'b' 중에서 골라야 합니다` -- half translated, which reads worse than either language whole. */
function koreanList(english: string): string {
  return english.replace(/ or /g, " 또는 ")
}

const RULES: Rule[] = [
  { match: /^Field required$/, korean: "반드시 입력해야 합니다" },
  { match: /^Extra inputs are not permitted$/, korean: "이 항목은 사용할 수 없습니다" },
  { match: /^String should have at least (\d+) characters?$/, korean: "$1자 이상이어야 합니다" },
  { match: /^String should have at most (\d+) characters?$/, korean: "$1자 이하여야 합니다" },
  { match: /^Input should be greater than or equal to (.+)$/, korean: "$1 이상이어야 합니다" },
  { match: /^Input should be less than or equal to (.+)$/, korean: "$1 이하여야 합니다" },
  { match: /^Input should be greater than (.+)$/, korean: "$1보다 커야 합니다" },
  { match: /^Input should be less than (.+)$/, korean: "$1보다 작아야 합니다" },
  { match: /^Input should be a valid integer.*$/, korean: "정수를 입력해 주세요" },
  { match: /^Input should be a valid number.*$/, korean: "숫자를 입력해 주세요" },
  { match: /^Input should be a valid string$/, korean: "문자열을 입력해 주세요" },
  { match: /^Input should be a valid boolean.*$/, korean: "참 또는 거짓이어야 합니다" },
  { match: /^Input should be a valid list$/, korean: "목록이어야 합니다" },
  { match: /^Input should be a valid dictionary.*$/, korean: "객체여야 합니다" },
  { match: /^Input should be (.+)$/, korean: (list) => `${koreanList(list)} 중에서 골라야 합니다` },
  { match: /^List should have at least (\d+) items?.*$/, korean: "항목이 $1개 이상이어야 합니다" },
  { match: /^List should have at most (\d+) items?.*$/, korean: "항목이 $1개 이하여야 합니다" },
  { match: /^String should match pattern '(.+)'$/, korean: "형식이 맞지 않습니다 (허용: $1)" },
  { match: /^Value error, (.+)$/, korean: "$1" },
]

/** The English half of a message the engine composed as `<무엇>: <pydantic>`. */
const COMPOSED = /^([^:]+):\s(.+)$/s

function translateSentence(english: string): string | null {
  for (const rule of RULES) {
    const found = rule.match.exec(english)
    if (found !== null) {
      const captures = found.slice(1).map((capture) => capture ?? "")
      return typeof rule.korean === "function"
        ? rule.korean(...captures)
        : rule.korean.replace(/\$(\d+)/g, (_, index: string) => found[Number(index)] ?? "")
    }
  }
  return null
}

/**
 * The message to show for an issue.
 *
 * Unchanged when the engine already wrote Korean, and unchanged when the English is a sentence this
 * does not know -- **never empty**. Showing nothing because a translation is missing turns a message
 * someone could have acted on into a silent failure.
 */
export function koreanMessage(message: string): string {
  const composed = COMPOSED.exec(message)
  if (composed !== null) {
    const [, what, rest] = composed
    const korean = translateSentence(rest ?? "")
    if (korean !== null) return `${what}: ${korean}`
  }
  const whole = translateSentence(message)
  return whole ?? message
}
