import type { NextConfig } from "next"

const nextConfig: NextConfig = {
  // The container copies `.next/standalone`, which carries its own minimal `node_modules`. Without
  // this the image would need the whole pnpm store to run what it already built.
  output: "standalone",
  // `apps/web` is not the repository root, and Next's tracing walks up looking for a lockfile. Saying
  // where the app is keeps the standalone bundle from reaching into the engine's tree.
  outputFileTracingRoot: import.meta.dirname,
}

export default nextConfig
