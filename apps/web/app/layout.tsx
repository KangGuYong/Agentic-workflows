import type { Metadata } from "next"
import { IBM_Plex_Mono, IBM_Plex_Sans_KR } from "next/font/google"

import "./globals.css"

// IBM Plex, not Inter/Geist/Arial: Plex was drawn as an engineering typeface and it has the
// characterful details (the angled terminals, the flat-sided 'a') that a neutral UI sans deliberately
// lacks. The KR cut covers Korean, which is this product's first language, so headings and body never
// split across two unrelated families the way a Latin-only display face would force.
//
// `preload: false` is load-bearing, not a default we forgot to change. Google splits a Korean face into
// ~100 unicode-range subsets per weight, and next/font emits a <link rel="preload"> for every file it
// self-hosts: measured at **376 preload links in the built HTML** with preload on. The browser needs a
// handful of those subsets for any given page and picks them by unicode-range on its own, so preloading
// is all cost. Turning it off drops the count to the mono face's own handful. See the Task 1 note.
const plexKr = IBM_Plex_Sans_KR({
  variable: "--font-plex-kr",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  display: "swap",
  preload: false,
})

// Readouts only: node ids, exec_index/attempt, durations, token counts. All ASCII by construction.
const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  display: "swap",
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
