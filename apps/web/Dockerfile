# syntax=docker/dockerfile:1
# Same posture as the engine image: multi-stage, a slim runtime, and a non-root uid that owns nothing
# it can write to.

FROM node:22-bookworm-slim AS deps
WORKDIR /app
ENV PNPM_HOME=/pnpm PATH=/pnpm:$PATH
RUN corepack enable
# Dependencies first: they change far less often than the source, so this layer stays cached.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
# Playwright's browsers are a test-time concern; the image must not download 300MB it never runs.
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
RUN --mount=type=cache,target=/pnpm/store pnpm install --frozen-lockfile

FROM node:22-bookworm-slim AS build
WORKDIR /app
ENV PNPM_HOME=/pnpm PATH=/pnpm:$PATH PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
RUN corepack enable
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# `instrumentation.ts` validates the engine env at startup, not at build time, so no secret is needed
# here -- and none is passed: a build argument would end up in the image's history.
RUN pnpm build

FROM node:22-bookworm-slim AS runtime
# The editor proxies untrusted tenant requests and renders untrusted tenant strings. Neither has any
# reason to be able to write to the image or to run as uid 0.
RUN useradd --create-home --uid 10001 web
WORKDIR /app
ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0

# `standalone` carries the server and the minimal module graph it actually needs; `static` and `public`
# are served from disk beside it and are not part of that graph.
COPY --from=build --chown=web:web /app/.next/standalone ./
COPY --from=build --chown=web:web /app/.next/static ./.next/static
COPY --from=build --chown=web:web /app/public ./public

USER web
EXPOSE 3000
CMD ["node", "server.js"]
