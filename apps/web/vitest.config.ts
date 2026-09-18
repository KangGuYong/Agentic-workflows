import react from "@vitejs/plugin-react"
import { defineConfig } from "vitest/config"

// Two projects, one `pnpm test`. `lib/` is pure logic and runs on node -- it must never need a DOM,
// which is the point of keeping the DSL commands, the template parser and the error mapping in there.
// Components run on jsdom. Plan 3 conventions 4: neither needs Docker; only `pnpm e2e` does.
//
// Projects inherit this file's `plugins` and `resolve`, so neither is repeated below; Vitest 5 warns
// when they are. `@/` comes from tsconfig's paths via Vite's own resolution, not a plugin.
export default defineConfig({
  resolve: {
    tsconfigPaths: true,
    alias: { "server-only": new URL("./test/server-only-stub.ts", import.meta.url).pathname },
  },
  test: {
    projects: [
      {
        test: { name: "lib", environment: "node", include: ["lib/**/*.test.ts", "store/**/*.test.ts"] },
      },
      {
        // Route handlers are Web-API code, not React: node, no DOM, no setup file.
        test: { name: "routes", environment: "node", include: ["app/api/**/*.test.ts"] },
      },
      {
        plugins: [react()],
        test: {
          name: "components",
          environment: "jsdom",
          setupFiles: ["./vitest.setup.ts"],
          include: ["app/**/*.test.tsx", "components/**/*.test.tsx"],
        },
      },
    ],
  },
})
