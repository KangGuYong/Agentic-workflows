import "@testing-library/jest-dom/vitest"

import { cleanup } from "@testing-library/react"
import { afterEach } from "vitest"

// Testing Library registers its own afterEach cleanup only when the framework's globals are injected,
// and this project does not set `globals: true`. Without this, each render stacks on the last and
// `getAllByRole` quietly returns the previous test's DOM as well as this one's.
afterEach(cleanup)

// jsdom implements `<dialog>` as an element but not `showModal`/`close`, so a component that opens one
// throws on mount. The shim below makes the element *open*, which is what these tests assert about.
//
// It does NOT make it modal: focus trapping, the inert background and Esc are exactly the parts jsdom
// has none of, and they are the parts that matter for a dialog holding a destructive choice. Those are
// asserted in a real browser instead -- see `e2e/conflict-dialog.spec.ts`.
if (typeof HTMLDialogElement !== "undefined" && HTMLDialogElement.prototype.showModal === undefined) {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true
  }
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.open = false
  }
}
