import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { STATUSES } from "@/lib/design/status"

import Page from "./page"

describe("the skeleton page", () => {
  it("shows every run status with its Korean label", () => {
    render(<Page />)
    for (const status of STATUSES) {
      expect(screen.getByText(status.label)).toBeInTheDocument()
    }
  })

  it("draws each status from its token rather than a literal colour", () => {
    const { container } = render(<Page />)
    const swatches = container.querySelectorAll<HTMLElement>("[style*='--st-']")
    expect(swatches).toHaveLength(STATUSES.length)
  })
})
