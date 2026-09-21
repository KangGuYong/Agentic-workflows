"use client"

/** The template rules, for someone who has never written Jinja (3 설계 §6.2).
 *
 * These are the engine's rules, not Jinja's -- `engine/templates/parser.py` rejects most of Jinja and
 * `engine/templates/env.py` allows a short list of filters. **This is a copy, so it goes stale if the
 * engine changes its rules.** Task 22 links it to `services/engine/README.md` from both sides.
 *
 * Collapsed by default: it is seven rules, and someone editing their tenth template does not need them
 * pushing the canvas off the screen.
 */

const RULES: { text: string; example?: string }[] = [
  { text: "값을 이으려면 나란히 씁니다.", example: "{{ a }}{{ b }}" },
  { text: "산술은 숫자끼리만 됩니다." },
  { text: "반복문은 중첩할 수 없습니다." },
  { text: "없는 값에 기본값을 주려면 default를 씁니다. null까지 바꾸려면 두 번째 인자를 켭니다.", example: "{{ a | default('-', true) }}" },
  { text: "JSON 템플릿에서는 {{ }} 하나가 JSON 값 하나입니다. 따옴표로 감싸지 마세요.", example: '{"name": {{ start.name }}}' },
  { text: "렌더된 값 하나는 최대 1,000,000자입니다." },
]

export function TemplateHelp() {
  return (
    <details className="mt-2">
      <summary className="cursor-pointer text-xs text-fg-faint select-none">템플릿 작성 규칙</summary>
      <ul className="mt-2 flex flex-col gap-1.5 text-xs text-fg-muted">
        {RULES.map((rule) => (
          <li key={rule.text}>
            {rule.text}
            {rule.example !== undefined ? (
              <code className="mt-0.5 block font-mono text-[11px] text-fg-faint">{rule.example}</code>
            ) : null}
          </li>
        ))}
      </ul>
    </details>
  )
}
