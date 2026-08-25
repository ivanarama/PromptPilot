#!/usr/bin/env bash
# Action «enqueue»: открывает popup-панель постановки задачи,
# пробрасывая туда контекст панели-источника (cwd и т.п.).
set -eu

herdr=${HERDR_BIN_PATH:-herdr}
ctx=${HERDR_PLUGIN_CONTEXT_JSON:-}

args=(plugin pane open --plugin promptpilot --entrypoint enqueue)

if [ -n "$ctx" ]; then
    args+=(--env "HERDR_PLUGIN_CONTEXT_JSON=$ctx")
    if command -v jq > /dev/null 2>&1; then
        cwd=$(printf '%s' "$ctx" | jq -r '.cwd // empty' 2> /dev/null || true)
    else
        cwd=$(printf '%s' "$ctx" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("cwd") or "")
except Exception:
    pass' 2> /dev/null || true)
    fi
    if [ -n "${cwd:-}" ]; then
        args+=(--cwd "$cwd")
    fi
fi

exec "$herdr" "${args[@]}"
