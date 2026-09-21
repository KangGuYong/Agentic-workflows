import { describe, expect, it } from "vitest"

import { koreanMessage } from "./korean"

describe("koreanMessage", () => {
  it("leaves a message the engine already wrote in Korean alone", () => {
    // Most issues are the engine's own words. Touching them would be a second, worse translation.
    const message = "'llm_1' 노드가 항상 먼저 실행된다는 보장이 없습니다. | default(...)를 붙이세요: llm_1.text"

    expect(koreanMessage(message)).toBe(message)
  })

  it("translates the pydantic half of a composed message, keeping the engine's half", () => {
    // `engine/validator/structure.py::pydantic_issues` composes `설정 오류: <pydantic's sentence>`.
    expect(koreanMessage("설정 오류: Field required")).toBe("설정 오류: 반드시 입력해야 합니다")
    expect(koreanMessage("실행 정책 오류: Extra inputs are not permitted")).toBe(
      "실행 정책 오류: 이 항목은 사용할 수 없습니다",
    )
  })

  it("carries the number out of the sentence", () => {
    // "at least 1 character" and "at least 5 characters" are different messages to a person.
    expect(koreanMessage("설정 오류: String should have at least 1 character")).toBe(
      "설정 오류: 1자 이상이어야 합니다",
    )
    expect(koreanMessage("설정 오류: String should have at most 200 characters")).toBe(
      "설정 오류: 200자 이하여야 합니다",
    )
    expect(koreanMessage("설정 오류: Input should be greater than or equal to 0")).toBe(
      "설정 오류: 0 이상이어야 합니다",
    )
  })

  it("translates each pydantic sentence the engine's models can produce", () => {
    // Captured by running the engine's own pydantic against a model with these constraints; the exact
    // strings are pinned in the engine's suite, where an upgrade would change them.
    const cases: [string, string][] = [
      ["Field required", "반드시 입력해야 합니다"],
      ["Extra inputs are not permitted", "이 항목은 사용할 수 없습니다"],
      ["String should have at least 1 character", "1자 이상이어야 합니다"],
      ["String should have at most 5 characters", "5자 이하여야 합니다"],
      ["Input should be greater than or equal to 0", "0 이상이어야 합니다"],
      ["Input should be less than or equal to 2", "2 이하여야 합니다"],
      ["Input should be a valid number, unable to parse string as a number", "숫자를 입력해 주세요"],
      ["Input should be a valid integer, unable to parse string as an integer", "정수를 입력해 주세요"],
      ["Input should be 'a' or 'b'", "'a' 또는 'b' 중에서 골라야 합니다"],
      ["Input should be 'GET', 'POST' or 'PATCH'", "'GET', 'POST' 또는 'PATCH' 중에서 골라야 합니다"],
    ]

    for (const [english, korean] of cases) {
      expect(koreanMessage(`설정 오류: ${english}`), english).toBe(`설정 오류: ${korean}`)
    }
  })

  it("shows the English original for a sentence it does not know, never an empty string", () => {
    // A missing translation must degrade to something a person can still act on. Blanking the message
    // would turn a fixable problem into a silent one.
    const unknown = "설정 오류: Some future pydantic sentence"

    expect(koreanMessage(unknown)).toBe(unknown)
  })

  it("translates a bare pydantic sentence with no prefix", () => {
    expect(koreanMessage("Field required")).toBe("반드시 입력해야 합니다")
  })

  it("does not mistake a Korean message containing a colon for a composed one", () => {
    // `REF_UNKNOWN_FIELD` reads `'llm_1' 노드 출력에 없는 필드입니다: llm_1.nope`.
    const message = "'llm_1' 노드 출력에 없는 필드입니다: llm_1.nope"

    expect(koreanMessage(message)).toBe(message)
  })

  it("splits at the first colon, so a colon inside the message survives", () => {
    // A custom validator's Korean sentence may itself contain a colon. Splitting at the last one would
    // hand the rules a fragment that matches nothing, and the translation would silently stop working
    // for exactly the messages the engine wrote most deliberately.
    expect(koreanMessage("설정 오류: Value error, 시간: 형식이 올바르지 않습니다")).toBe(
      "설정 오류: 시간: 형식이 올바르지 않습니다",
    )
  })

  it("passes a custom validator's own words through", () => {
    // Pydantic prefixes these with `Value error, `; the rest is the engine's message and is Korean.
    expect(koreanMessage("설정 오류: Value error, 조건이 하나 이상 필요합니다")).toBe(
      "설정 오류: 조건이 하나 이상 필요합니다",
    )
  })

  it("is never empty for any input", () => {
    for (const message of ["", ":", "a: ", "설정 오류: "]) {
      expect(koreanMessage(message), JSON.stringify(message)).toBe(message)
    }
  })
})
