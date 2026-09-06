#!/usr/bin/env bash

# Code version: v1.2.0-codex.1

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/resolve_python.sh"

if ! PYTHON_BIN="$(resolve_python_bin runtime)"; then
	echo "Python 3.13 or newer with the required runtime dependencies not found: ${AGENTIC_CONTEXT_PYTHON:-${CACHELIKES_PYTHON:-host python3}}" >&2
	echo "Install requirements.txt into the selected environment, or set AGENTIC_CONTEXT_PYTHON to a prepared interpreter." >&2
	exit 1
fi

cd "$ROOT_DIR"
exec "$PYTHON_BIN" main.py "$@"
