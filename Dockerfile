# syntax=docker/dockerfile:1.7
# Hangar API image: python:3.13-slim + uv, runs as a non-root user.
# The same image runs the API (default CMD) and the worker (`hangar worker`).

FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8100 \
    STORAGE_DIR=/app/storage \
    LOG_FORMAT=json
RUN groupadd --system --gid 10001 hangar \
    && useradd --system --uid 10001 --gid hangar --home-dir /app --shell /usr/sbin/nologin hangar
WORKDIR /app
COPY --from=builder --chown=hangar:hangar /app /app
RUN mkdir -p /app/storage && chown hangar:hangar /app/storage
USER hangar
EXPOSE 8100
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT', '8100')}/api/v1/health\", timeout=4)"]
CMD ["hangar", "serve"]
