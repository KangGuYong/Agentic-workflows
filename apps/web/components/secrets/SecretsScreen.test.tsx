import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type { SecretSummary } from "@/lib/engine/secrets"

import { SecretsScreen } from "./SecretsScreen"

const ROW: SecretSummary = {
  name: "API_KEY",
  createdAt: "2026-09-01T00:00:00.000Z",
  updatedAt: "2026-09-20T00:00:00.000Z",
}

let calls: { url: string; init?: RequestInit }[]

function answer(...responses: Response[]) {
  const queue = [...responses]
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init })
      return Promise.resolve(queue.shift() ?? new Response(JSON.stringify({ secrets: [ROW] }), { status: 200 }))
    }),
  )
}

beforeEach(() => {
  calls = []
  answer()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("the write-only rule", () => {
  it("shows the name and when it changed, and nothing that stands for the value", () => {
    // No `••••••` where a value would be: that reads as "it is here, just hidden". The API has no
    // endpoint that reads a value back, and the screen must not imply there could be one.
    const { container } = render(<SecretsScreen initial={[ROW]} />)

    expect(screen.getByText("API_KEY")).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/[•*]{3,}/)
    expect(screen.queryByRole("button", { name: /보기|표시/ })).toBeNull()
  })

  it("says plainly that a saved value cannot be seen again", () => {
    render(<SecretsScreen initial={[]} />)

    expect(screen.getByText(/다시 볼 수 없습니다/)).toBeInTheDocument()
  })

  it("clears the value box after saving", async () => {
    // Leaving a secret in a form field keeps it in the DOM, in autofill, and in any screenshot taken
    // after.
    answer(new Response(null, { status: 204 }), new Response(JSON.stringify({ secrets: [ROW] }), { status: 200 }))
    render(<SecretsScreen initial={[]} />)

    await userEvent.type(screen.getByLabelText("이름"), "NEW_KEY")
    const value = screen.getByLabelText("값")
    await userEvent.type(value, "sk-live-abcdefgh")
    await userEvent.click(screen.getByRole("button", { name: "저장" }))

    expect((value as HTMLInputElement).value).toBe("")
  })

  it("does not put the value in the DOM as plain text", async () => {
    render(<SecretsScreen initial={[]} />)
    await userEvent.type(screen.getByLabelText("값"), "sk-live-abcdefgh")

    expect(screen.getByLabelText("값")).toHaveAttribute("type", "password")
    expect(screen.getByLabelText("값")).toHaveAttribute("autocomplete", "off")
  })
})

describe("the name field", () => {
  it("explains what is wrong instead of waiting for the engine's 404", async () => {
    // The engine answers a malformed name with a 404 by design; "찾을 수 없습니다" does not say that
    // lowercase is the problem.
    render(<SecretsScreen initial={[]} />)
    await userEvent.type(screen.getByLabelText("이름"), "api_key")

    expect(screen.getByText(/소문자/)).toBeInTheDocument()
  })

  it("offers the obvious correction", async () => {
    render(<SecretsScreen initial={[]} />)
    await userEvent.type(screen.getByLabelText("이름"), "api_key")
    await userEvent.click(screen.getByRole("button", { name: /API_KEY/ }))

    expect(screen.getByLabelText("이름")).toHaveValue("API_KEY")
  })

  it("will not save a name the engine would refuse", async () => {
    render(<SecretsScreen initial={[]} />)
    await userEvent.type(screen.getByLabelText("이름"), "api_key")
    await userEvent.type(screen.getByLabelText("값"), "sk-live-abcdefgh")

    expect(screen.getByRole("button", { name: "저장" })).toBeDisabled()
  })

  it("will not save a value the engine would refuse", async () => {
    render(<SecretsScreen initial={[]} />)
    await userEvent.type(screen.getByLabelText("이름"), "API_KEY")
    await userEvent.type(screen.getByLabelText("값"), "short")

    expect(screen.getByText(/8자 이상/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "저장" })).toBeDisabled()
  })
})

describe("replacing a value", () => {
  it("says so, and the button changes with it", async () => {
    // Saving over a secret every workflow depends on is not the same act as adding a new one.
    render(<SecretsScreen initial={[ROW]} />)
    await userEvent.type(screen.getByLabelText("이름"), "API_KEY")
    await userEvent.type(screen.getByLabelText("값"), "sk-live-abcdefgh")

    expect(screen.getByText(/이전 값을 덮어씁니다/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "값 교체" })).toBeInTheDocument()
  })
})

describe("deleting", () => {
  it("asks first, and says what it costs", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false)
    render(<SecretsScreen initial={[ROW]} />)
    await userEvent.click(screen.getByRole("button", { name: "삭제" }))

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("실행에 실패합니다"))
    expect(calls.some((call) => call.init?.method === "DELETE")).toBe(false)
    confirm.mockRestore()
  })

  it("deletes when confirmed", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true)
    answer(new Response(null, { status: 204 }), new Response(JSON.stringify({ secrets: [] }), { status: 200 }))
    render(<SecretsScreen initial={[ROW]} />)
    await userEvent.click(screen.getByRole("button", { name: "삭제" }))

    expect(calls[0]?.url).toBe("/api/engine/secrets/API_KEY")
    expect(calls[0]?.init?.method).toBe("DELETE")
    confirm.mockRestore()
  })
})
