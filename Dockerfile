# PromptPilot — server image (worker / server / bot)
# Build:  docker compose build
# Run:    docker compose up
#
# All three PromptPilot services share a single SQLite database.
# In Docker each service runs as its own process — see docker-compose.yml.

FROM python:3.11-slim

# System packages commonly required by AI CLIs that the worker may spawn
# (ripgrep for Cursor Agent, git for Claude Code, curl/ca-certificates for auth).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ripgrep \
        git \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 + Claude Code CLI.
# The worker spawns `claude` as a subprocess, so the CLI must exist in the image.
# Authentication is handled at runtime via ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN
# (set in docker-compose), so no interactive `claude auth login` is required.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code
ENV PP_CLAUDE_EXE=/usr/bin/claude

WORKDIR /app

# Install the Python package
COPY pyproject.toml ./
COPY promptpilot ./promptpilot
RUN pip install --no-cache-dir .

# Persistent data directory holds the SQLite DB and config files.
# Mount a volume here (see docker-compose.yml) so tasks survive restarts.
ENV PP_DATA_DIR=/data
# Bind the web server on all interfaces inside the container.
ENV PP_HOST=0.0.0.0
ENV PP_PORT=8420

RUN mkdir -p /data
VOLUME /data

# ENTRYPOINT is the `pp` console script; compose sets the subcommand per service.
ENTRYPOINT ["pp"]
CMD ["server"]
