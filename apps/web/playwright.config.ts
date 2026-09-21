import { defineConfig } from "@playwright/test"

/** Browser tests, for the things a jsdom test cannot answer.
 *
 * Right now that is one thing: IME composition in the template editor (3 설계 §6.1, Task 10). jsdom has
 * no composition, no layout and no real selection, so a unit test of the editor can only assert that it
 * mounted. Task 21 adds the full editor flows on top of this config.
 */
const PREINSTALLED_CHROMIUM = process.env.PLAYWRIGHT_CHROMIUM_PATH

export default defineConfig({
  testDir: "./e2e",
  // A hang is a failure. Nothing here waits on anything slower than a local dev server.
  timeout: 30_000,
  expect: { timeout: 5_000 },
  forbidOnly: process.env.CI !== undefined,
  reporter: process.env.CI !== undefined ? "list" : "line",
  use: { baseURL: "http://127.0.0.1:5199", trace: "retain-on-failure" },
  webServer: {
    command: "pnpm exec vite --config vite.fixture.config.ts",
    url: "http://127.0.0.1:5199",
    reuseExistingServer: process.env.CI === undefined,
    timeout: 60_000,
  },
  projects: [
    {
      name: "chromium",
      // An environment that already has a browser (a CI image, a sandbox) points at it here rather than
      // having Playwright download a second copy. Unset, Playwright resolves its own as usual.
      use: PREINSTALLED_CHROMIUM === undefined
        ? {}
        : { launchOptions: { executablePath: PREINSTALLED_CHROMIUM } },
    },
  ],
})
