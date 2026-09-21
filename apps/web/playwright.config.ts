import { defineConfig } from "@playwright/test"

/** Browser tests, in two groups that need different things running.
 *
 * **component** mounts one component on a Vite page (`e2e/fixture`). It answers what jsdom cannot --
 * IME composition, dialog modality -- and needs nothing but a browser. Playwright starts its server.
 *
 * **stack** drives the real editor against the real engine, Postgres and Redis (3 설계 §11).
 * Playwright does **not** start that: it is five containers, and a test runner that brought them up
 * would be starting a deployment rather than testing one. The operator or CI starts it with
 * `docker compose up -d` and these tests connect.
 */
const FIXTURE_URL = "http://127.0.0.1:5199"
const STACK_URL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:3000"
const PREINSTALLED_CHROMIUM = process.env.PLAYWRIGHT_CHROMIUM_PATH

export default defineConfig({
  testDir: "./e2e",
  // A hang is a failure. Nothing here waits on anything slower than a local container.
  timeout: 60_000,
  expect: { timeout: 10_000 },
  forbidOnly: process.env.CI !== undefined,
  reporter: process.env.CI !== undefined ? "list" : "line",
  // Each spec makes its own workflow and deletes it, so they do not collide. One worker anyway: the
  // stack is one engine with one worker, and racing runs through it tests the runner, not the editor.
  workers: 1,
  webServer: {
    command: "pnpm exec vite --config vite.fixture.config.ts",
    url: FIXTURE_URL,
    reuseExistingServer: process.env.CI === undefined,
    timeout: 60_000,
  },
  projects: [
    {
      name: "component",
      testDir: "./e2e/fixture",
      use: { baseURL: FIXTURE_URL, trace: "retain-on-failure", ...launch() },
    },
    {
      name: "stack",
      testDir: "./e2e/stack",
      use: { baseURL: STACK_URL, trace: "retain-on-failure", ...launch() },
    },
  ],
})

/** An environment that already has a browser (a CI image, a sandbox) points at it here rather than
 * having Playwright download a second copy. Unset, Playwright resolves its own as usual. */
function launch() {
  return PREINSTALLED_CHROMIUM === undefined
    ? {}
    : { launchOptions: { executablePath: PREINSTALLED_CHROMIUM } }
}
