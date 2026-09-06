#!/usr/bin/env bash

# Code version: v1.1.2-codex.1

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/resolve_python.sh"

if ! PYTHON_BIN="$(resolve_python_bin)"; then
	echo "Supported Python 3.13 or newer interpreter not found: ${AGENTIC_CONTEXT_PYTHON:-${CACHELIKES_PYTHON:-host python3}}" >&2
	echo "Set AGENTIC_CONTEXT_PYTHON only when an explicit supported interpreter is required." >&2
	exit 1
fi

echo "Using Python: $PYTHON_BIN"
"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -r "$ROOT_DIR/requirements-dev.txt"

if [[ "${AGENTIC_CONTEXT_SKIP_PLAYWRIGHT_INSTALL:-${CACHELIKES_SKIP_PLAYWRIGHT_INSTALL:-0}}" != "1" ]]; then
	"$PYTHON_BIN" -m playwright install chromium
fi

echo
echo "Environment is ready."
echo "Run tests with: $ROOT_DIR/scripts/test.sh"
echo "Run the quality gate with: $ROOT_DIR/scripts/check.sh"
echo "Run the app with: $ROOT_DIR/scripts/run_app.sh"
