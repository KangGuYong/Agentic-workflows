// `server-only` throws unless it is resolved inside a React Server Component build. Vitest is not that
// build, so importing a server module under test would fail on the guard rather than on anything real.
// Aliasing it away here does not weaken the guarantee: what actually keeps the token out of the browser
// is the Next build (which still resolves the real package) and `bundle.test.ts`, which greps the built
// client chunks for the token itself.
export {}
