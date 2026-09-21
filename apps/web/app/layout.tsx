import type { Metadata } from "next"
import localFont from "next/font/local"

import "./globals.css"

// IBM Plex, not Inter/Geist/Arial: Plex was drawn as an engineering typeface and it has the
// characterful details (the angled terminals, the flat-sided 'a') that a neutral UI sans deliberately
// lacks. The KR cut covers Korean, which is this product's first language, so headings and body never
// split across two unrelated families the way a Latin-only display face would force.
//
// **Self-hosted, not `next/font/google`.** That loader fetches from fonts.googleapis.com **at build
// time**, so `next build` fails in any network that cannot reach Google -- which is precisely the
// network this product is deployed into. It was only found when the container was first built
// (Task 20); a dev machine behind a permissive proxy never sees it. The files come from IBM's own
// OFL-licensed packages, which the build already reaches a registry for.
//
// One file per weight rather than the ~100 unicode-range subsets Google splits a Korean face into.
// The complete Korean face is 438KB per weight in woff2 -- small enough that the subsetting machinery
// (and the 376 preload links it produced, see the Task 1 note) costs more than it saves.
const plexKr = localFont({
  variable: "--font-plex-kr",
  display: "swap",
  src: [
    { path: "../node_modules/@ibm/plex-sans-kr/fonts/complete/woff2/hinted/IBMPlexSansKR-Regular.woff2", weight: "400", style: "normal" },
    { path: "../node_modules/@ibm/plex-sans-kr/fonts/complete/woff2/hinted/IBMPlexSansKR-SemiBold.woff2", weight: "600", style: "normal" },
    { path: "../node_modules/@ibm/plex-sans-kr/fonts/complete/woff2/hinted/IBMPlexSansKR-Bold.woff2", weight: "700", style: "normal" },
  ],
})

// Readouts only: node ids, exec_index/attempt, durations, token counts. All ASCII by construction,
// which is why the mono face needs no Korean coverage and is a tenth the size.
const plexMono = localFont({
  variable: "--font-plex-mono",
  display: "swap",
  src: [
    { path: "../node_modules/@ibm/plex-mono/fonts/complete/woff2/IBMPlexMono-Regular.woff2", weight: "400", style: "normal" },
    { path: "../node_modules/@ibm/plex-mono/fonts/complete/woff2/IBMPlexMono-Medium.woff2", weight: "500", style: "normal" },
  ],
})

export const metadata: Metadata = {
  title: "워크플로 빌더",
  description: "LLM 워크플로를 그리고, 돌리고, 지켜보는 편집기",
}

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="ko" className={`${plexKr.variable} ${plexMono.variable} h-full antialiased`}>
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  )
}
