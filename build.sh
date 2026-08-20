#!/usr/bin/env bash
# Build standalone pp binary with PyInstaller (Linux/macOS)
# Usage: ./build.sh           — normal build
#        ./build.sh --debug   — verbose output for troubleshooting

set -euo pipefail

DEBUG=0
if [[ "${1:-}" == "--debug" || "${1:-}" == "-d" ]]; then
    DEBUG=1
fi

echo "=== PromptPilot Build (Linux) ==="

# Install pyinstaller if missing
if ! python3 -m PyInstaller --version >/dev/null 2>&1; then
    echo "Installing PyInstaller..."
    python3 -m pip install pyinstaller
fi

# Clean previous build
rm -rf dist build

# Build
if [[ "$DEBUG" -eq 1 ]]; then
    echo "Building pp (debug)..."
    python3 -m PyInstaller pp.spec --clean --debug all
else
    echo "Building pp..."
    python3 -m PyInstaller pp.spec --clean
fi

if [[ -f "dist/pp" ]]; then
    size=$(du -h "dist/pp" | cut -f1)
    echo ""
    echo "Done: dist/pp ($size)"
    echo "Test: ./dist/pp --help"
else
    echo "Build failed." >&2
    exit 1
fi
