# PromptPilot — server image (worker / server / bot)
# Build:  docker compose build
# Run:    docker compose up
#
# All three PromptPilot services share a single SQLite database.
# In Docker each service runs as its own process — see docker-compose.yml.

FROM python:3.11-slim

# System packages commonly required by AI CLIs the worker may spawn
# (ripgrep for Cursor Agent, git for Claude Code, curl/ca-certificates for auth).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ripgrep \
        git \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 + Claude Code CLI. The worker spawns `claude` as a subprocess, so
# the CLI must exist in the image. Auth is handled at runtime via
# ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN (set in docker-compose), or by
# mounting ~/.claude from the host (see README) — no interactive login needed.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code
ENV PP_CLAUDE_EXE=/usr/bin/claude

WORKDIR /app

# Install the Python package. README.md is required: pyproject declares
# readme = "README.md", so setuptools reads it while building metadata.
COPY pyproject.toml README.md ./
COPY promptpilot ./promptpilot
RUN pip install --no-cache-dir .

# Run as a non-root user. Claude Code REFUSES to run with
# --dangerously-skip-permissions as root, so an autonomous worker under root
# would fail every skip-permissions task. uid 1000 keeps volume files sane.
RUN useradd --create-home --uid 1000 pp \
    && mkdir -p /data \
    && chown -R pp:pp /data /app
USER pp
ENV HOME=/home/pp

# Persistent data directory holds the SQLite DB and config files. Mount a volume
# here (see docker-compose.yml) so tasks survive restarts.
ENV PP_DATA_DIR=/data
# Bind on all interfaces INSIDE the container (required for Docker port publish).
# The real security boundary is the host port mapping, so opt out of the
# loopback guard here — keep the compose publish on 127.0.0.1 unless you also
# set PP_API_TOKEN. See docker-compose.yml.
ENV PP_HOST=0.0.0.0
ENV PP_PORT=8420
ENV PP_ALLOW_INSECURE_BIND=1

VOLUME /data

# ENTRYPOINT is the `pp` console script; compose sets the subcommand per service.
ENTRYPOINT ["pp"]
CMD ["server"]
