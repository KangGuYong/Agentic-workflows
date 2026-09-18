# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.8-python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# Dependencies first: they change far less often than the source, so this layer stays cached.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project --no-dev
COPY engine ./engine
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm AS runtime
# The render pool forks worker processes and the API serves untrusted templates: neither has any reason to
# be able to write to the image or to run as uid 0.
RUN useradd --create-home --uid 10001 engine
WORKDIR /app
COPY --from=build --chown=engine:engine /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER engine
CMD ["python", "-m", "engine.api.main"]
