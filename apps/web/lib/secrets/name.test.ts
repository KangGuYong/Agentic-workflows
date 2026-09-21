import { describe, expect, it } from "vitest"

import { MAX_NAME_LENGTH, MAX_VALUE_LENGTH, MIN_VALUE_LENGTH, nameProblem, suggestName, valueProblem } from "./name"

describe("nameProblem", () => {
  it("accepts what the engine accepts", () => {
    for (const name of ["A", "API_KEY", "GITHUB_TOKEN_2", "X".repeat(MAX_NAME_LENGTH)]) {
      expect(nameProblem(name), name).toBeNull()
    }
  })

  it("rejects what the engine rejects", () => {
    // `^[A-Z][A-Z0-9_]{0,63}$` -- the same rule in `api/routers/secrets.py` and `secrets/markers.py`.
    for (const name of ["", "a", "api_key", "1KEY", "_KEY", "API-KEY", "API KEY", "키", "X".repeat(65)]) {
      expect(nameProblem(name), JSON.stringify(name)).not.toBeNull()
    }
  })

  it("says what is wrong, not that something is", () => {
    // The engine answers a malformed name with a 404 by design. "찾을 수 없습니다" does not tell someone
    // that lowercase is the problem.
    expect(nameProblem("api_key")).toContain("소문자")
    expect(nameProblem("1KEY")).toContain("시작")
    expect(nameProblem("API-KEY")).toContain("밑줄")
    expect(nameProblem("")).toContain("입력")
    expect(nameProblem("X".repeat(65))).toContain("64자")
  })

  it("blames the first thing wrong, most specific first", () => {
    // A name that is both lowercase and starts with a digit is fixed by fixing the start first.
    expect(nameProblem("1key")).toContain("시작")
  })
})

describe("valueProblem", () => {
  it("accepts a value within the engine's bounds", () => {
    expect(valueProblem("X".repeat(MIN_VALUE_LENGTH))).toBeNull()
    expect(valueProblem("X".repeat(MAX_VALUE_LENGTH))).toBeNull()
  })

  it("rejects one that is too short", () => {
    // The engine's floor exists so that value-based redaction cannot scrub unrelated text.
    expect(valueProblem("X".repeat(MIN_VALUE_LENGTH - 1))).toContain("8자 이상")
    expect(valueProblem("")).not.toBeNull()
  })

  it("rejects one that is too long", () => {
    expect(valueProblem("X".repeat(MAX_VALUE_LENGTH + 1))).toContain("4096자 이하")
  })

  it("never repeats the value back", () => {
    // An error echoing what was sent would put the secret in the message, and from there into whatever
    // logged it.
    const secret = "sk-live-abcdef"
    expect(valueProblem(secret.slice(0, 3))).not.toContain(secret.slice(0, 3))
  })
})

describe("suggestName", () => {
  it("offers the obvious correction", () => {
    expect(suggestName("api_key")).toBe("API_KEY")
    expect(suggestName("github-token")).toBe("GITHUB_TOKEN")
    expect(suggestName("1api")).toBe("API")
  })

  it("offers nothing when the name is already fine", () => {
    expect(suggestName("API_KEY")).toBeNull()
  })

  it("offers nothing when the correction would still be wrong", () => {
    // A suggestion the tenant would have to fix again is noise.
    expect(suggestName("")).toBeNull()
    expect(suggestName("123")).toBeNull()
    expect(suggestName("___")).toBeNull()
  })

  it("keeps the correction inside the length bound", () => {
    const suggested = suggestName("x".repeat(100))

    expect(suggested === null || suggested.length <= MAX_NAME_LENGTH).toBe(true)
  })
})
