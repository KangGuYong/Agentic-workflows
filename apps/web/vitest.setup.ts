import "@testing-library/jest-dom/vitest"

import { cleanup } from "@testing-library/react"
import { afterEach } from "vitest"

// Testing Library registers its own afterEach cleanup only when the framework's globals are injected,
// and this project does not set `globals: true`. Without this, each render stacks on the last and
// `getAllByRole` quietly returns the previous test's DOM as well as this one's.
afterEach(cleanup)
