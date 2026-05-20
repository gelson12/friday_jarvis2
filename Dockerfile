# syntax=docker/dockerfile:1.6
#
# Single-container build: the Python LiveKit voice worker AND the Next.js
# Friday UI run side-by-side in one Railway service. Mirrors the proven
# co-location pattern from OpenJarvis (livekit/start.sh): one deploy, one
# set of env vars, one public URL.
#
# Layout at runtime:
#   /app/agent.py                  ← Python LiveKit worker (no public port)
#   /app/prompts.py
#   /app/frontend/server.js        ← Next.js standalone server (binds $PORT)
#   /app/frontend/.next/static/
#   /app/frontend/public/
#   /app/start.sh                  ← launcher: runs both, exits when either dies
#
# Public port: only Next.js binds. Railway injects $PORT (default 3000).
# The worker connects outbound to LiveKit Cloud — no inbound port needed.

# ── Stage 1: build Next.js standalone bundle ────────────────────────
FROM node:20-alpine AS frontend-build
ENV PNPM_HOME="/pnpm" \
    PATH="/pnpm:$PATH" \
    NEXT_TELEMETRY_DISABLED=1
RUN corepack enable && corepack prepare pnpm@9.15.9 --activate
WORKDIR /app/frontend

# Cache deps layer separately from source so unrelated source edits
# don't re-run pnpm install.
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

# Build (next.config.ts already sets output: 'standalone')
COPY frontend/ ./
RUN pnpm build

# ── Stage 2: combined Python + Node runtime ─────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000

# System deps: Python build toolchain (some wheels still compile),
# Node 20 runtime for `node server.js`, tini for proper PID 1 signal
# forwarding, git for any VCS pip deps, curl/ca-certificates.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg gcc g++ build-essential git tini \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps cached separately
COPY requirements.txt ./
RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Pre-warm Silero VAD so the first voice session has no model-download stall
RUN python -c "from livekit.plugins import silero; silero.VAD.load()" 2>/dev/null || true

# Worker code (explicit list — no COPY . . sprawl)
COPY agent.py prompts.py hermes_adapter.py ./
COPY thirdparty/ ./thirdparty/

# Next.js standalone artifacts. Standalone is a minimised tree; static
# assets and public/ must be copied as siblings at their canonical paths.
COPY --from=frontend-build /app/frontend/.next/standalone /app/frontend
COPY --from=frontend-build /app/frontend/.next/static /app/frontend/.next/static
COPY --from=frontend-build /app/frontend/public /app/frontend/public

# Launcher
COPY start.sh ./
RUN chmod +x start.sh

EXPOSE 3000

# tini as PID 1 → forwards SIGTERM cleanly to start.sh, start.sh forwards
# to both child processes. No zombie processes; clean Railway restarts.
#
# start.sh is part of ENTRYPOINT (not CMD) so Railway's "Custom Start
# Command" dashboard field — which overrides CMD — cannot bypass the
# dual-process launcher. Any args it appends become $@ to start.sh,
# which the launcher ignores.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/start.sh"]
CMD []
