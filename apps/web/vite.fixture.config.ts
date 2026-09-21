import { fileURLToPath } from "node:url"

import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

/** Serves `e2e/fixture` for the Playwright tests. Not part of the app build. */
export default defineConfig({
  root: fileURLToPath(new URL("./e2e/fixture", import.meta.url)),
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL(".", import.meta.url)) } },
  server: { port: 5199, strictPort: true },
  // One page per component under test, so a failure names the component rather than the fixture app.
  build: { rollupOptions: { input: ["index.html", "conflict.html"] } },
})
