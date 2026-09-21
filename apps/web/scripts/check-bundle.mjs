#!/usr/bin/env node
/**
 * Prove the engine token cannot reach the browser (3 설계 §3.3).
 *
 * The obvious version of this check -- grep the client chunks for the token, pass if absent -- proves
 * nothing, and its control case is what showed that: **Next never inlines a server-side `process.env.X`
 * at all.** Only `NEXT_PUBLIC_*` is substituted at build time, and only where code actually reads it;
 * everything else stays a runtime lookup. The token therefore appears in no build output whatever, and
 * the grep would pass just as happily with `server-only` deleted and the token read from a client
 * component -- the *value* still would not be in the bundle, only the code reading it.
 *
 * So this checks the two things that can actually break, each with a control that must fire:
 *
 *   1. The grep works. A client component that reads a `NEXT_PUBLIC_` sentinel puts it in the client
 *      chunks, so the same search must find it there. If it does not, finding nothing else means nothing.
 *   2. `server-only` bites. The same page, rewritten to import `lib/engine/env`, must fail the build.
 *      That import guard is what keeps the reading code off the browser, so it is tested by breaking it.
 *
 * The probe lives at `app/bundle-probe/` deliberately: a folder starting with `_` is an App Router
 * *private folder* and is never routed, so a probe placed there is silently not built -- which is how
 * check 2 first "passed" while testing nothing.
 *
 * Run with `pnpm test:bundle`; kept out of `pnpm test` because each check is a production build.
 */
import { execFileSync } from "node:child_process"
import { mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs"
import { join } from "node:path"

const PUBLIC_SENTINEL = "public-sentinel-4c1f9a"
const TOKEN_SENTINEL = "engine-token-sentinel-9f3a1c"
const ROOT = process.cwd()
const PROBE_DIR = join(ROOT, "app/bundle-probe")
const PROBE_PAGE = join(PROBE_DIR, "page.tsx")

const READS_PUBLIC_ENV = `"use client"

export default function BundleProbe() {
  return <p>{process.env.NEXT_PUBLIC_BUNDLE_PROBE}</p>
}
`

const IMPORTS_SERVER_ONLY_MODULE = `"use client"

import { engineEnv } from "@/lib/engine/env"

export default function BundleProbe() {
  return <p>{String(engineEnv().baseUrl)}</p>
}
`

function walk(dir) {
  let entries
  try {
    entries = readdirSync(dir)
  } catch {
    return []
  }
  return entries.flatMap((entry) => {
    const path = join(dir, entry)
    return statSync(path).isDirectory() ? walk(path) : [path]
  })
}

function chunksContaining(dir, needle) {
  return walk(dir)
    .filter((path) => /\.(js|mjs|map)$/.test(path))
    .filter((path) => readFileSync(path, "utf8").includes(needle))
}

function build({ quiet = false } = {}) {
  try {
    execFileSync("node_modules/.bin/next", ["build"], {
      cwd: ROOT,
      stdio: quiet ? "pipe" : "inherit",
      env: {
        ...process.env,
        ENGINE_API_URL: "http://api:8000",
        ENGINE_API_TOKEN: TOKEN_SENTINEL,
        NEXT_PUBLIC_BUNDLE_PROBE: PUBLIC_SENTINEL,
      },
    })
    return { ok: true, output: "" }
  } catch (error) {
    return { ok: false, output: `${error.stdout ?? ""}${error.stderr ?? ""}` }
  }
}

const failures = []
mkdirSync(PROBE_DIR, { recursive: true })

try {
  // ── 1. the token is absent from the client bundle, and the search demonstrably works ────────────────
  console.log("\n[1/2] building a client page that reads a NEXT_PUBLIC_ sentinel ...")
  writeFileSync(PROBE_PAGE, READS_PUBLIC_ENV)
  if (!build().ok) {
    failures.push("the sentinel build failed; nothing below was checked")
  } else {
    const control = chunksContaining(join(ROOT, ".next/static"), PUBLIC_SENTINEL)
    if (control.length === 0) {
      failures.push(
        "control: the NEXT_PUBLIC_ sentinel is NOT in .next/static, so this search proves nothing " +
          "(wrong path, or the probe page was not built)",
      )
    } else {
      console.log(`  control ok: it is in ${control.length} client chunk(s), as it must be`)
    }
    const leaked = chunksContaining(join(ROOT, ".next/static"), TOKEN_SENTINEL)
    if (leaked.length > 0) {
      failures.push(`the engine token is in ${leaked.length} client chunk(s): ${leaked.join(", ")}`)
    } else {
      console.log("  ok: the engine token is in 0 client chunks")
    }
  }

  // ── 2. server-only refuses the same page once it imports the server module ──────────────────────────
  console.log("\n[2/2] rewriting that page to import lib/engine/env from the client ...")
  writeFileSync(PROBE_PAGE, IMPORTS_SERVER_ONLY_MODULE)
  const probe = build({ quiet: true })
  if (probe.ok) {
    failures.push("a client component imported lib/engine/env and the build SUCCEEDED -- server-only is not guarding")
  } else if (!/server-only|Server Component/i.test(probe.output)) {
    failures.push(`the probe build failed for some other reason:\n${probe.output.slice(-800)}`)
  } else {
    console.log("  ok: the build refused it")
  }
} finally {
  rmSync(PROBE_DIR, { recursive: true, force: true })
  // Next writes `.next/types/validator.ts` with an entry per route, so the probe leaves a reference to a
  // page that no longer exists and the next `pnpm typecheck` fails with TS2307 on a file nobody wrote.
  // One more build regenerates it. A check script must not leave the tree in a state where the next
  // command fails.
  console.log("\nregenerating route types ...")
  build({ quiet: true })
}

if (failures.length > 0) {
  console.error("\nFAIL")
  for (const failure of failures) console.error(`  - ${failure}`)
  process.exit(1)
}
console.log("\nOK: the engine token cannot reach the browser.")
